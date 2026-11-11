"""Checklist and Test Specifier: one LLM call per table and per table pair turns the accepted dimensions (and SQL
guarantees) into TestSpecifications; an optional repair pass sends rejected ones back with the validator's reason."""
import json

from dqvouch.context import _for_prompt
from dqvouch.llm import extract_dq_json
from dqvouch.profiling import DQ_DIMENSIONS


REPAIR_PER_SCOPE = 8  # specs sent back per call, so the repair prompt stays small


def _signals_for(asset_contexts, tables, dimension):
    """dq_signals keys of `tables` that imply `dimension` -- for a single table, its signals of
    that dimension; for a pair, the signals that name BOTH tables (row-preserving copy,
    upstream filter). Never adds a dimension the Dimension Selector did not accept."""
    keys = []
    for t in tables:
        for key, sig in ((asset_contexts or {}).get(t, {}).get("dq_signals") or {}).items():
            if str(sig.get("dimension", "")).lower() != str(dimension).lower():
                continue
            named = set(sig.get("tables") or []) | {sig.get("upstream"), sig.get("consumer"), t} - {None}
            if len(tables) == 1 or set(tables) <= named:
                keys.append(key)
    return list(dict.fromkeys(keys))  # extraction order: no ranking of which facts matter most


def _signals_note(asset_contexts, tables, dimension) -> str:
    keys = _signals_for(asset_contexts, tables, dimension)
    if not keys:
        return ""
    return (" -- related dq_signals facts (evidence to weigh, not tests to copy; propose a test from one only "
            "if it implies a rule the data must satisfy): " + ", ".join(keys))


def _build_recommendation_checklist(accepted_dimension_json: dict, asset_contexts: dict = None):
    """
    Renders the accepted dimension recommendations as an explicit numbered
    checklist instead of a nested JSON blob for the Test Specifier to parse.

    Observed behavior (real run, 2026-08-03): given 22 accepted dimension
    recommendations with profiling evidence available for nearly all of
    them, the model wrote TestSpecifications for only 8 -- not because
    evidence was missing (it wasn't -- 67 profiling entries existed to draw
    on), but because an open-ended "here's a JSON structure, produce specs
    for it" framing is easy for a smaller/faster model to summarize a subset
    of rather than exhaustively work through, even with an explicit "cover
    everything" rule stated in prose elsewhere in the prompt. A flat,
    numbered, "you must address every line" checklist is a much harder
    format to silently skip items from.

    Returns (checklist_text, item_count).
    """
    lines = []
    n = 0
    for table_entry in accepted_dimension_json.get("tables", []):
        table = table_entry.get("table", "?")
        for dim in table_entry.get("dimensions", []):
            n += 1
            cols = ", ".join(dim.get("columns", []))
            lines.append(
                f"{n}. [{table}] {dim.get('dimension')} -- columns: {cols} -- "
                f"rationale: {dim.get('rationale', '')}"
                + _signals_note(asset_contexts, [table], dim.get("dimension"))
            )
    for check in accepted_dimension_json.get("cross_table_checks", []) or []:
        n += 1
        tables = " vs ".join(check.get("tables_involved", []))
        cols = ", ".join(check.get("columns_involved", []))
        lines.append(
            f"{n}. [CROSS-TABLE: {tables}] {check.get('dimension')} -- columns: {cols} -- "
            f"rationale: {check.get('rationale', '')}"
            + _signals_note(asset_contexts, check.get("tables_involved", []), check.get("dimension"))
        )
    return "\n".join(lines), n


# ---------------------------------------------------------------------------
# Per-asset Test Specifier support.
#
# The Dimension Selector stays a single call over every table (it needs
# simultaneous visibility across tables to spot cross-layer signals at all
# -- see the "Cross-layer shared columns" rule in build_dimension_selector_
# prompt). The Test Specifier does not have that requirement for its
# single-table assertion types (row_predicate, unique) -- each of those only
# ever references ONE table's own columns -- so it's split into several
# smaller, scoped calls instead: one per table with at least one accepted
# dimension recommendation, plus one per unique PAIR of tables with at least
# one accepted cross_table_check (those still need two tables' AssetContext
# visible at once to validate join_key/columns -- see
# validator.validate_test_specifications's
# CROSS_ASSET_ASSERTION_TYPES handling). See run_test_specifier_per_asset()
# below for the orchestration.
# ---------------------------------------------------------------------------

def _group_cross_table_checks_by_pair(cross_table_checks) -> dict:
    """
    Groups accepted cross_table_checks entries by the exact pair of tables
    they involve (order-independent), so run_test_specifier_per_asset() can
    issue one Test Specifier call per PAIR rather than one per individual
    check -- e.g. a Consistency row-count check AND a Consistency
    missing-rows check between the same two tables still only cost one call.
    """
    pairs = {}
    for check in cross_table_checks or []:
        tables = check.get("tables_involved", [])
        if len(tables) != 2:
            continue  # malformed -- validate_dimension_output should already reject this upstream
        key = tuple(sorted(tables))
        pairs.setdefault(key, []).append(check)
    return pairs


def _profiling_summary_for_asset(profiling_summary, asset_full_name: str):
    """
    Slices a full-run profiling_summary (see build_profiling_summary) down to
    just the entries for ONE asset, matched on the bare table name (the part
    after the layer prefix) -- profiling_results never records the layer
    separately (see parse_check_id / run_profiling), only
    the bare table_name, so that's the only thing available to match on.
    Correct as long as table names are unique across layers (the same bare
    name never reused in two schemas). Cross-table entries
    (whose "table" field is a compound "t1_vs_t2" string) never equal a bare
    single-table name, so they're naturally excluded here already.
    """
    if not profiling_summary:
        return profiling_summary
    bare_table = asset_full_name.split(".", 1)[-1]
    return {cid: entry for cid, entry in profiling_summary.items() if entry.get("table") == bare_table}


def _profiling_summary_for_pair(profiling_summary, table_a: str, table_b: str):
    """
    Slices a full-run profiling_summary down to just the cross-table entries
    for ONE pair of tables -- those carry a compound "table" field shaped
    "<table1>_vs_<table2>" (see profiling.py's cross-table check_id
    format), matched here order-independently against the pair.
    """
    if not profiling_summary:
        return profiling_summary
    wanted = {table_a, table_b}
    result = {}
    for cid, entry in profiling_summary.items():
        compound = entry.get("table", "")
        if "_vs_" in compound and set(compound.split("_vs_")) == wanted:
            result[cid] = entry
    return result


def _namespace_spec_ids(specs: list, prefix: str) -> list:
    """
    Rewrites each spec's own `id` field in place to "<prefix>__<original
    id>" and returns the same list. Needed because run_test_specifier_per_
    asset() makes several INDEPENDENT Gemini calls instead of one -- each
    call only ever sees its own table (or table pair), so nothing stops two
    separate calls from both naming a spec e.g. "completeness_check". The
    Output Contract Validator (validate_test_specifications) rejects a
    second spec with an id it's already seen as a "duplicate specification
    id" -- correct behavior for a genuine duplicate, but a false positive
    here, since these would come from unrelated tables. Namespacing by the
    call's own scope guarantees global uniqueness without depending on the
    LLM's own "short_unique_slug" instruction holding across calls that
    never see each other's output. Only `id` is touched -- a
    MetricThreshold's `check_id` must stay byte-for-byte identical to the
    profiling check_id it references, so that field is never touched here.
    """
    for spec in specs:
        if spec.get("id"):
            spec["id"] = f"{prefix}__{spec['id']}"
    return specs


def _test_specifier_call(scope_label, scoped_context, scoped_dim_json, scoped_profiling, id_prefix, llm,
                         prompt_suffix="", engine="ClickHouse"):
    """One scoped Test Specifier call: prompt -> Gemini -> parsed specs (ids namespaced with id_prefix).
    Returns (specs, metric_thresholds, call_entry); a response that cannot be parsed twice yields no specs."""
    prompt = build_test_specifier_prompt(scoped_context, scoped_dim_json, scoped_profiling, engine) + prompt_suffix
    response = llm(prompt)
    print(f"\n=== Test Specifier Output [{scope_label}] ===\n")
    print(response)

    call_entry = {"scope": scope_label, "prompt": prompt, "raw_response": response}
    try:
        try:
            output = extract_dq_json(response)
        except ValueError as first_error:
            # Malformed JSON is a random sampling failure (a dropped comma, a
            # truncated object), not a property of the prompt: one retry of the
            # same prompt recovers the scope instead of losing all of its tests.
            print(f"WARNING: Test Specifier response for [{scope_label}] could not be parsed "
                  f"({first_error}) -- retrying once.")
            call_entry["first_attempt_error"] = str(first_error)
            call_entry["first_attempt_raw_response"] = response
            response = llm(prompt)
            print(response)
            call_entry["raw_response"] = response
            output = extract_dq_json(response)
    except ValueError as e:
        print(f"WARNING: Test Specifier response for [{scope_label}] could not be parsed "
              f"({e}) -- treating as zero specifications for this scope.")
        call_entry["error"] = str(e)
        return [], [], call_entry
    return (_namespace_spec_ids(output.get("test_specifications", []), id_prefix),
            _namespace_spec_ids(output.get("metric_thresholds", []), id_prefix), call_entry)


def run_test_specifier_per_asset(asset_contexts: dict, dim_accepted: dict, profiling_summary: dict, llm,
                                 engine: str = "ClickHouse", progress=None):
    """
    Runs the Test Specifier stage as several SCOPED Gemini calls instead of
    one combined call over every table at once: one call per table that has
    at least one accepted dimension recommendation, plus one call per unique
    pair of tables that has at least one accepted cross_table_check (see
    _group_cross_table_checks_by_pair above). Each call only receives that
    table's (or pair's) own slice of asset_contexts/profiling_summary and
    its own recommendation checklist -- a much shorter, more focused prompt
    than the single all-tables version this replaced.

    Rationale: the checklist-skipping problem already documented on
    _build_recommendation_checklist's docstring ("22 recommendations in,
    only 8 specs out") gets worse, not better, as more unrelated tables'
    items compete for attention in one pass. Splitting by asset keeps each
    call's checklist to what one table (typically <= 6 dimension entries)
    actually needs, at the cost of more Gemini calls per run.

    Cross-table checks are deliberately not folded into the per-asset calls
    (a single-table call can't validate a join_key or column that lives in a
    DIFFERENT table) and are not dropped either -- each unique pair gets its
    own small, dedicated call with just the two relevant tables' context.

    A per-call JSON-parse failure (extract_dq_json raising ValueError -- a
    malformed or missing ```json``` block from the model) is caught and
    logged rather than aborting the whole run: with N+M calls now made
    instead of one, one bad response from a single table shouldn't cost the
    other tables' (or the other two context levels', when run_all_levels is
    on) results too.

    Returns (test_specs_json, metric_thresholds_json, calls_record) --
    calls_record is a list of {"scope", "prompt", "raw_response"} dicts
    (plus "error" when that call's response failed to parse), one per Gemini
    call made, for save_run_manifest()'s reproducibility record.
    """
    test_specs_json = []
    metric_thresholds_json = []
    calls_record = []

    def _run_one_call(scope_label, scoped_context, scoped_dim_json, scoped_profiling, id_prefix):
        specs, thresholds, call_entry = _test_specifier_call(scope_label, scoped_context, scoped_dim_json,
                                                             scoped_profiling, id_prefix, llm, engine=engine)
        test_specs_json.extend(specs)
        metric_thresholds_json.extend(thresholds)
        calls_record.append(call_entry)

    progress = progress or (lambda message: None)
    scopes = []
    for table_entry in dim_accepted.get("tables", []):
        table = table_entry.get("table")
        if table not in asset_contexts:
            continue  # already rejected upstream by validate_dimension_output if genuinely unknown
        scopes.append(dict(
            scope_label=table,
            scoped_context={table: asset_contexts[table]},
            scoped_dim_json={"tables": [table_entry]},
            scoped_profiling=_profiling_summary_for_asset(profiling_summary, table),
            id_prefix=table,
        ))

    pairs = _group_cross_table_checks_by_pair(dim_accepted.get("cross_table_checks", []))
    for (table_a, table_b), checks in pairs.items():
        if table_a not in asset_contexts or table_b not in asset_contexts:
            continue  # already rejected upstream by validate_dimension_output if genuinely unknown
        scopes.append(dict(
            scope_label=f"CROSS-TABLE: {table_a} vs {table_b}",
            scoped_context={table_a: asset_contexts[table_a], table_b: asset_contexts[table_b]},
            scoped_dim_json={"tables": [], "cross_table_checks": checks},
            scoped_profiling=_profiling_summary_for_pair(profiling_summary, table_a, table_b),
            id_prefix=f"{table_a}+{table_b}",
        ))

    for i, scope in enumerate(scopes, 1):
        progress(f"Test Specifier: {scope['scope_label']} ({i} of {len(scopes)})")
        _run_one_call(**scope)

    return test_specs_json, metric_thresholds_json, calls_record


def _scope_inputs(scope: str, asset_contexts: dict, dims: dict, profiling_summary: dict):
    """(context, checklist json, profiling slice) of one Test Specifier scope, as run_test_specifier_per_asset
    built it: a table name, or "<table_a>+<table_b>" for a cross-table pair (the prefix of the spec ids)."""
    if "+" in scope:
        a, b = scope.split("+", 1)
        checks = _group_cross_table_checks_by_pair(dims.get("cross_table_checks", [])).get(tuple(sorted((a, b))), [])
        return ({a: asset_contexts[a], b: asset_contexts[b]}, {"tables": [], "cross_table_checks": checks},
                _profiling_summary_for_pair(profiling_summary, a, b))
    entry = next((t for t in dims.get("tables", []) if t.get("table") == scope), {"table": scope, "dimensions": []})
    return {scope: asset_contexts[scope]}, {"tables": [entry]}, _profiling_summary_for_asset(profiling_summary, scope)


def _repairable(rejected: list, specs_by_id: dict) -> dict:
    """{scope: [(spec, reason)]} for the rejections a rewrite can fix -- every reason but a duplicate."""
    out = {}
    for r in rejected:
        spec = specs_by_id.get(r.get("scope"))
        if spec is None or str(r.get("reason", "")).startswith("duplicate") or "__repair__" in spec.get("id", ""):
            continue
        out.setdefault(spec["id"].split("__", 1)[0], []).append((spec, r.get("reason", "")))
    return out


def run_repair_pass(asset_contexts: dict, dims: dict, profiling_summary: dict, rejected: list, specs: list, llm,
                    engine: str = "ClickHouse", progress=None):
    """One extra Test Specifier call per scope with fixable rejections: the rejected specifications go back with
    the validator's reason and the model rewrites or drops them. Returns (specs, thresholds, calls)."""
    by_id = {s.get("id"): s for s in specs if isinstance(s, dict)}
    progress = progress or (lambda message: None)
    new_specs, new_thresholds, calls = [], [], []
    todo_scopes = [(scope, items) for scope, items in _repairable(rejected, by_id).items()
                   if all(t in asset_contexts for t in scope.split("+"))]
    for i, (scope, items) in enumerate(todo_scopes, 1):
        progress(f"Repair: {scope} ({i} of {len(todo_scopes)})")
        keys = ("asset", "title", "dimension", "assertion", "threshold", "evidence")
        todo = [f"- {spec['id']} -- rejected because: {reason}\n  {json.dumps({k: spec.get(k) for k in keys})[:1500]}"
                for spec, reason in items[:REPAIR_PER_SCOPE]]
        suffix = ("\n\n## Repair pass\n\nThe Output Contract Validator rejected the specifications below, each for the "
                  "reason given. Rewrite each one so it keeps its intent and fixes that problem (use only columns and "
                  "tables present in the AssetContext; for two tables, join on columns that exist on both sides and "
                  "filter each table with a condition tree on its own columns), or leave it out if it cannot be fixed. "
                  "Output ONLY the rewritten specifications.\n" + "\n".join(todo))
        ctx, scoped_dims, prof = _scope_inputs(scope, asset_contexts, dims, profiling_summary)
        s, t, c = _test_specifier_call(f"{scope} (repair)", ctx, scoped_dims, prof, f"{scope}__repair", llm, suffix,
                                       engine)
        c["repaired"] = [spec["id"] for spec, _ in items[:REPAIR_PER_SCOPE]]
        new_specs += s
        new_thresholds += t
        calls.append(c)
    return new_specs, new_thresholds, calls


# ---------------------------------------------------------------------------
# Test Specifier: takes the SAME AssetContext used for dimension selection
# plus the already-validated DimensionRecommendations, and asks for concrete
# TestSpecifications. It is ALSO given the profiling output computed earlier
# in the same run, and may cite it as evidence (the profile is evidence, not
# a rule: a threshold still needs a cited fact).
# ---------------------------------------------------------------------------

_ASSERTION_CATALOG_TABLE = """
| assertion.type | Use it for | Required fields |
|---|---|---|
| row_predicate | a rule that should hold for every row -- covers not-null checks, value ranges, fixed-value domains, single-row conditional rules (including column-to-column comparisons), and comparisons against the current moment, all in one general shape | condition: a boolean expression tree. Leaf: {op: eq\\|neq\\|gt\\|gte\\|lt\\|lte, left: {column}\\|{value}\\|{function}, right: {column}\\|{value}\\|{function}} (either side may be a column, a literal, or -- ONLY for "must not be in the future" / "must not be later than now" style rules -- {function: now} against a DateTime column or {function: today} against a Date column; e.g. end_date >= start_date is column-vs-column, timestamp <= now is column-vs-function). Also: {op: is_null\\|is_not_null, left: {column}}, {op: in\\|not_in, left: {column}, values: [literal,...]}, {op: and\\|or, clauses: [node, node, ...]} (>=2 clauses), {op: implies, if: node, then: node} for "whenever X, then Y" rules within the same row. A side may also apply a function to ONE column: {function: length\\|domain\\|trim\\|lower, column: <col>} (length = number of elements of an array, or characters of a string; domain = host of a URL; trim/lower normalize text) -- usable on either side of a comparison and as the left side of is_null/in |
| unique | a column (or column combination) must have no duplicates | columns: [str] |
| cross_asset_row_count | two assets should have a specific row-count relationship | tables: [str, str]; optional filters (see below) |
| cross_asset_missing_rows | rows in one asset should all have a match in another, by a shared key | tables: [str, str], join_key (see below); optional filters |
| cross_asset_column_equality | a specific column's VALUE must still match across two assets for the same logical row -- not just that the row exists (cross_asset_missing_rows) or that the two tables' sizes agree (cross_asset_row_count), but that a value carried from one layer to the next (a hash, a category, a derived key) was not silently altered in transit | tables: [str, str], join_key (see below), columns: [col_in_table1, col_in_table2] (same column name on both sides is fine -- repeat it); optional filters |
"""


def build_test_specifier_prompt(asset_contexts: dict, accepted_dimension_json: dict, profiling_summary: dict = None,
                                engine: str = "ClickHouse") -> str:
    # Built as plain strings first (single braces, literal) and interpolated
    # into the f-string below as a single {slot} -- keeps this from repeating
    # the earlier bug where literal JSON braces inside an f-string got
    # mis-parsed as format expressions.
    # evidence has to be rationale instead of a simple description.
    recommendation_checklist, recommendation_count = _build_recommendation_checklist(accepted_dimension_json, asset_contexts)
    # Only when this scope carries SQL guarantees (sql_rules on), so prompts are unchanged otherwise.
    invariant_rule = (
        "\n- SQL guarantees (`sql_invariants`): checklist items whose rationale starts with \"SQL guarantee\" were "
        "extracted from the transformation SQL, and their quoted excerpt was checked against it. For each one, write at "
        "least one specification that verifies the data satisfies it, citing \"sql_invariants.<id>\" as evidence. The "
        "quoted SQL excerpt is an explicit basis for a threshold of equals 0: the transformation enforces the rule, so any "
        "violating row is a defect. You choose the assertion; the usual shapes are: row_provenance -> "
        "cross_asset_missing_rows with tables [output, input] (every output row has its input row) and the input filtered "
        "by the condition; row_completeness -> cross_asset_row_count, or cross_asset_missing_rows from input to output; "
        "value_domain -> row_predicate `in` (inside `implies` when it holds only under a condition; an `or` of `and`s for "
        "combinations of columns); presence -> row_predicate is_not_null / != '' (inside `implies` under a condition); "
        "grain -> unique on the key; branch_source -> cross_asset_missing_rows with the output filtered by the marker; "
        "for a guarantee between two tables, join on the column pairs it lists (\"same row on both sides\"), each pair "
        "written in the order of your `tables`, and put each condition in `filters` under the table whose columns it uses; "
        "input_values -> row_predicate `in` on that input (inside `implies` under a condition); input_function -> "
        "row_predicate on {function, column} != '' when the function is in the catalog (inside `implies` under a "
        "condition), otherwise leave it out; input_presence -> is_not_null / != '' (inside `implies` under a condition). "
        "When the rationale says the output is a UNION ALL, put that branch condition in `filters` under the output "
        "table. derivation guarantees are context only (not checklist items). Never copy a run parameter or placeholder of the SQL "
        "template (e.g. {load_date:Date}, 'EXECUTION_DATE') into a condition or a filter: it selects the load, it is not a value."
        if any(c.get("sql_invariants") for c in asset_contexts.values()) else ""
    )

    threshold_basis = (
        "the AssetContext (a column/table description, transformation SQL, or a column's own "
        "declared type -- e.g. an Enum's fixed member list, or Bool's true/false domain) "
        "or, when provided below, the Observed Data Profile"
        if profiling_summary else
        "the AssetContext (a column/table description, transformation SQL, or a column's own "
        "declared type -- e.g. an Enum's fixed member list, or Bool's true/false domain)"
    )
    evidence_path_hint = (
        '(e.g. "columns.score.description", "columns.classification.type" when the TYPE itself '
        'is the basis, or "profile:<check_id>" when using the data profile)'
        if profiling_summary else
        '(e.g. "columns.score.description", or "columns.classification.type" when the TYPE itself is the basis)'
    )

    profile_rule = ""
    profile_section = ""
    metric_threshold_rule = ""
    if profiling_summary:
        profile_rule = (
            "\n- You are ALSO given an **Observed Data Profile** below: actual computed values "
            "(null counts, cardinality, observed distributions, outlier counts, etc.) from running "
            "profiling queries against the real data just now, earlier in this same run. Unlike the "
            "AssetContext, this reflects a single snapshot of the current data, not a documented "
            "business rule -- treat it as supporting evidence, not ground truth. You MAY use it to "
            "set or refine a threshold (e.g. an observed value list with no unexpected entries can "
            "justify a row_predicate 'in' condition; a near-zero observed violation/outlier count can "
            'support a row_predicate range or is_not_null condition) by citing it as evidence with '
            '{"path": "profile:<check_id>"} '
            "(use the exact check_id shown for that entry below). Prefer citing an AssetContext path "
            "when a documented rule exists -- it's more durable than one observed snapshot."
        )
        profile_section = (
            "\n\n## Observed Data Profile (informational -- see note in Strict rules above; "
            "a single snapshot, not a documented rule)\n"
            "<DATA_PROFILE>\n"
            + json.dumps(profiling_summary, indent=2, default=str) +
            "\n</DATA_PROFILE>"
        )
        metric_threshold_rule = (
            "\n- MetricThreshold -- a SECOND, lighter-weight kind of check, for when a checklist "
            "item's only real evidence is a profiling metric that does NOT fit any of the "
            "TestSpecification assertion types above (e.g. Validity's distinct_ratio_pct, "
            "Accuracy's outliers_3stddev or negative_count, Freshness's hours_since_last_update, "
            "Consistency's functional_dependency violation counts, Validity's array-length stats -- "
            "NOT a plain future-value check on a timestamp, which is a row_predicate per the rule "
            "above). Instead of forcing an ill-fitting assertion type, put these in a "
            "SEPARATE `metric_thresholds` array in your output, shaped: "
            '{"id": "<slug>", "check_id": "<the exact check_id from the Observed Data Profile>", '
            '"metric": "<the exact metric field name from that check_id\'s rows>", '
            '"asset": "<layer>.<table>", "title": "<short title>", '
            '"operator": "<one of the threshold operators>", "value": <number>, '
            '"evidence": [{"path": "profile:<check_id>"}], "rationale": "<one sentence>", '
            '"status": "proposed"}. Both `check_id` and `metric` MUST come from an entry that '
            "literally appears in the Observed Data Profile below -- do not invent either. Unlike a "
            "TestSpecification, `operator` and `value` are REQUIRED here (there's no no-threshold "
            "form -- if you can't pin down a number, just don't propose a MetricThreshold for that "
            "item, propose it as an observation-only TestSpecification instead if one applies, or "
            "skip it per the YIELD rule's own escape hatch)."
        )

    metric_thresholds_schema_block = (
        """,
  "metric_thresholds": [
    {
      "id": "<short_unique_slug>",
      "check_id": "<exact check_id from the Observed Data Profile>",
      "metric": "<exact metric field name from that check_id's rows>",
      "asset": "<layer>.<table_name>",
      "title": "<short human-readable title>",
      "operator": "<one of the threshold operators>",
      "value": <number>,
      "evidence": [{"path": "profile:<check_id>"}],
      "rationale": "<one sentence>",
      "status": "proposed"
    }
  ]"""
        if profiling_summary else ""
    )
    metric_thresholds_omit_note = (
        " Omit `metric_thresholds` entirely (an empty array is fine too) when nothing in the "
        "Observed Data Profile warrants a metric-level threshold."
        if profiling_summary else ""
    )
    yield_output_kinds = (
        "a TestSpecification OR a MetricThreshold" if profiling_summary else "a TestSpecification"
    )
    yield_count_note = (
        "your test_specifications PLUS metric_thresholds entries combined"
        if profiling_summary else "your test_specifications entries"
    )

    return f"""You are a data quality expert. You previously identified which DQ dimensions apply to a set of {engine} tables. Your task now is to turn each accepted dimension recommendation into one or more concrete, executable TestSpecifications.

## Strict rules
- Use ONLY the assertion types in the catalog below. Do not invent new ones.
- Reference ONLY columns and tables that literally appear in the AssetContext provided.
- Do NOT write raw SQL or free-text expressions -- assertions are structured fields only, as shown in the catalog. The rendering code turns these structured fields into SQL; you never produce SQL text yourself.
- Set a `threshold` ONLY when you have an explicit, citable basis for the exact limit -- either {threshold_basis}. In that case, `evidence` MUST cite the exact path {evidence_path_hint} where the basis was found.{metric_threshold_rule}
- If no explicit basis exists, DO NOT set a threshold and DO NOT guess one from typical values, common sense, or statistics -- omit the `threshold` field entirely. This is the expected outcome when you genuinely have nothing to cite, not a failure.
- IMPORTANT -- do not stop at just writing the assertion's own parameters. For row_predicate, unique, cross_asset_missing_rows, and cross_asset_column_equality: if you're confident enough to cite evidence for the assertion itself (e.g. a range condition, an `in` values list, a dedup rule, a column that must carry through unchanged across layers), that confidence also justifies a threshold of {{"metric": "failed_rows", "operator": "equals", "value": 0}} (use "missing_count" instead of "failed_rows" for cross_asset_missing_rows, and "mismatched_count" for cross_asset_column_equality) -- ALWAYS include it in that case. Writing a condition bounding score between 0 and 1 as the assertion but leaving out the threshold produces a check that can never fail; that is a bug, not caution. Reserve leaving `threshold` out entirely for cross_asset_row_count, or any case where you truly cannot pin down even the assertion's own parameters from the evidence.{profile_rule}
- Every specification must include an `evidence` list citing at least one path that grounds why the test is being proposed at all, even when there is no threshold.
- Set `status` to "proposed" for every specification -- final acceptance is a human decision, not yours to make.
- YIELD -- READ THIS CAREFULLY: the checklist below has {recommendation_count} numbered items. You MUST produce at least one output -- {yield_output_kinds} -- for EVERY numbered item that has ANY grounding evidence available, in the AssetContext or (when provided) the Observed Data Profile -- do not silently skip an item just because you're not confident enough to also set a threshold, and do not stop early after covering a subset. A TestSpecification with no threshold is still a real, useful output (it executes and reports the observed value for a human to review) -- omitting the whole item is not the safe default, omitting just the threshold is. Work through the checklist in order, item by item. Before writing your final JSON, count {yield_count_note} against the {recommendation_count} checklist items -- if the count is lower, go back through the checklist and find what you skipped; only skip an item if you genuinely cannot find a single citable fact for it, and even then prefer writing an output with sparse evidence over omitting the item entirely.
- A `row_predicate` with an `implies` condition is the right tool whenever the Observed Data Profile includes a `grouped_numeric_stats` entry (a numeric column's min/max/avg broken out by a categorical column) that shows a clean, non-overlapping separation between two categories -- that separation is exactly what an `implies` condition should assert (e.g. {{"op": "implies", "if": {{"op": "eq", "left": {{"column": "category"}}, "right": {{"value": "X"}}}}, "then": {{"op": "gte", "left": {{"column": "numeric_column"}}, "right": {{"value": <the observed boundary>}}}}}}). Cite that check's `profile:<check_id>` as evidence.
- NOTE on what "threshold" means: for row_predicate, unique, cross_asset_missing_rows, and cross_asset_column_equality, `threshold` always answers "how many rows are allowed to violate this rule" -- which is legitimately 0 for a clean dataset, every time, regardless of the assertion. That is NOT where a varying numeric business value belongs -- those live in the assertion's OWN fields instead (row_predicate's condition -- comparison values, `in` values lists, etc.). Do not confuse the two: a threshold of {{"operator": "equals", "value": 0}} alongside a row_predicate condition bounding a column between 0 and 1 is two different numbers doing two different jobs, both correct.
- cross_asset_row_count is the one assertion type where `threshold` itself is expected to carry a real, non-zero/non-equals judgment call (e.g. a tolerated row-count drift) -- look for opportunities to propose it (matching table pairs that should track each other in size) even when you can't pin down the exact number yourself. It's correct to propose it with NO threshold in that case (per the YIELD rule above) and let a human set the tolerance in review -- don't skip proposing the specification just because you can't guess the number.
- A row_predicate range check accepts a one-sided bound (just a `gte` leaf, or just a `lte` leaf -- no need for an `and` wrapper) whenever the evidence only supports a floor or a ceiling, not both (e.g. "file_size must be positive" is a single {{"op": "gte", "left": {{"column": "file_size"}}, "right": {{"value": 0}}}} leaf; "hours_since_last_update should not exceed 24" is a single `lte` leaf). Do not invent the missing bound just to make a symmetric range.
- Column functions in a row_predicate: when a rule is about a derived property of a column rather than its raw value, use a {{"function": "length"|"domain"|"trim"|"lower", "column": "<col>"}} side -- e.g. two array columns that describe the same items position by position must have the same number of elements: {{"op": "eq", "left": {{"function": "length", "column": "<array_a>"}}, "right": {{"function": "length", "column": "<array_b>"}}}}; or a URL column must yield a non-empty domain: {{"op": "neq", "left": {{"function": "domain", "column": "<url_col>"}}, "right": {{"value": ""}}}}. No other functions exist -- do not invent any.
- For "must not be in the future" / "must not be later than now" rules, use a row_predicate `lte` leaf against {{"function": "now"}} (DateTime column) or {{"function": "today"}} (Date column) -- e.g. {{"op": "lte", "left": {{"column": "<col>"}}, "right": {{"function": "now"}}}} -- so it runs as a real per-row SQL check at test-run time. Do NOT propose this as a MetricThreshold: that would only report the future-row count as it stood at profiling time, not a live verdict. Reserve MetricThreshold for metrics that are genuinely aggregate and have no per-row equivalent at all (see the MetricThreshold rule below).
- NOTE on `semantic_roles` as evidence -- especially relevant when no description or transformation SQL is provided at all: a column may carry a `semantic_roles` hint inferred purely from its name (e.g. `boolean_flag` on `is_active`, `non_negative_measure` on `file_size`, `bounded_ratio_candidate` on `conversion_rate`). This is a real, citable fact (`"path": "columns.<col>.semantic_roles"`) but it is deliberately WEAK on its own -- a name-based convention, not a guarantee. Treat it as enough evidence to PROPOSE the specification (satisfying the YIELD rule), but do not also set a threshold from the role alone:
  - `boolean_flag` alone is enough to also set a threshold via a row_predicate `in` condition with values [true, false] (or [0, 1] matching the column's actual type) -- the domain a boolean-style name implies is concrete enough on its own.
  - `non_negative_measure` and `bounded_ratio_candidate` are NOT enough on their own to also set a threshold -- pair the role with the matching Observed Data Profile evidence when provided (e.g. a `profile:accuracy:<table>:<col>:sign` entry showing 0 observed negative values supports a `min=0` threshold; a `profile:accuracy:<table>:<col>` entry showing the actual observed min/max supports a range). If no profiling evidence is provided this run, propose the specification with the role cited but leave `threshold` out.
  - `candidate_business_key` is NOT enough on its own to justify a bare `unique(<that column>)` threshold of `equals 0` when the SAME table also has a column carrying the `temporal` role and there is no other evidence that this table is deduplicated (i.e. nothing in this table's own `transformation.raw_sql` shows a GROUP BY/DISTINCT/ROW_NUMBER-style reduction on that key). A business-key-shaped column sitting next to an event timestamp, with no dedup step producing this table, usually means the table is an append-only log where the same key legitimately recurs once per event (once per order, once per status change, etc.) -- asserting `unique(key)` there produces a check that fails constantly on correct data, not on real defects. In that situation, do one of: (a) propose `unique` over the composite of the business-key column(s) plus the temporal column instead of the bare key, if the evidence supports that being the real grain, or (b) do not propose a `unique` specification for that key at all (a `unique` without a threshold is rejected by validation, so it would be lost anyway). Only assert a bare-key `unique` threshold of equals 0 when there IS supporting dedup evidence: this table's own transformation performs a GROUP BY/DISTINCT/ROW_NUMBER-style reduction on that key, or (when profiling data is provided) a `profile:uniqueness:<table>:<col(s)>` entry shows 0 observed duplicates.

- Completeness of a String column is not only NULL: an empty string `''` is also a missing value. Include `<col> != ''` in the completeness condition whenever the column is a String identifier (`*_id`, `*_key`, `candidate_business_key` role) -- for a non-Nullable String this is the ONLY way a missing value can show up -- or the Observed Data Profile has a `blank_strings` entry for it (cite that `profile:` path).
- `dq_signals` and `downstream_usage`: an AssetContext may carry `dq_signals` -- facts extracted by code from the schema and from SQL, each stated in a `fact` field (what the code does, not which test to write) -- and `downstream_usage` -- the SQL of the assets that READ this table (look there for how its columns are used downstream: functions applied, filters, joins). Treat a dq_signals fact like any other context fact: decide yourself whether it implies a rule the data must satisfy and, if so, which assertion expresses it; cite "dq_signals.<key>" as evidence when you use one. The threshold rules above apply unchanged. For cross_asset_missing_rows / cross_asset_column_equality, use as join_key only identifier columns that are never null in both tables -- a NULL key never matches, so every such row would be reported missing.{invariant_rule}
## Assertion catalog
{_ASSERTION_CATALOG_TABLE}

## Output format
Respond ONLY with a short one-line summary per table, followed by a single JSON code block:

```json
{{
  "test_specifications": [
    {{
      "specification_version": "v1",
      "id": "<short_unique_slug>",
      "asset": "<layer>.<table_name>",
      "title": "<short human-readable title>",
      "dimension": "<the checklist item's dimension, one of: {', '.join(DQ_DIMENSIONS)}>",
      "scope": {{"type": "column", "columns": ["<col>"]}},
      "assertion": {{"type": "<one of the catalog types>", "...type-specific fields...": "..."}},
      "threshold": {{"metric": "failed_rows", "operator": "equals", "value": 0}},
      "evidence": [{{"path": "columns.<col>.description"}}], 
      "rationale": "<one sentence>",
      "status": "proposed"
    }}
  ]{metric_thresholds_schema_block}
}}
```

Omit the `threshold` key entirely (do not set it to null) when there is no explicit rule to cite.{metric_thresholds_omit_note}

## AssetContext
<ASSET_CONTEXT>
{json.dumps(_for_prompt(asset_contexts), indent=2)}
</ASSET_CONTEXT>

## Accepted dimension recommendations -- CHECKLIST (already validated -- input for deciding WHAT to specify)
There are {recommendation_count} numbered items below. Per the YIELD rule above, you must produce at least one TestSpecification for every item that has ANY citable evidence -- work through them in order, don't stop early.

<RECOMMENDATION_CHECKLIST>
{recommendation_checklist}
</RECOMMENDATION_CHECKLIST>{profile_section}"""
