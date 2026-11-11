"""Output Contract Validator: checks every LLM proposal against the AssetContext, the profiling facts and
the assertion catalog. It rejects what does not measure what it says, and flags (never deletes) a well-formed
rule the data contradicts."""
import json
import re
from dataclasses import dataclass, field

from dqvouch.profiling import metric_alias
from dqvouch.render_sql import (
    _ROW_PREDICATE_COLUMN_FUNCTIONS,
    _ROW_PREDICATE_FUNCTIONS,
    ASSERTION_RENDERERS,
    join_pairs,
)


@dataclass
class ValidationReport:
    accepted: dict
    rejected: list = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """True if nothing was rejected -- useful for quick run-summary checks."""
        return len(self.rejected) == 0

    def summary(self) -> str:
        n_tables = len(self.accepted.get("tables", []))
        n_dims = sum(len(t.get("dimensions", [])) for t in self.accepted.get("tables", []))
        n_cross = len(self.accepted.get("cross_table_checks", []))
        lines = [
            f"Output Contract Validator: {n_dims} dimension entries and "
            f"{n_cross} cross-table checks accepted across {n_tables} tables.",
        ]
        if self.rejected:
            lines.append(f"{len(self.rejected)} entries REJECTED:")
            for r in self.rejected:
                lines.append(f"  - [{r['scope']}] {r['reason']} -- {r.get('detail', '')}")
        return "\n".join(lines)


def _asset_lookup(asset_contexts) -> dict:
    """
    Normalizes `asset_contexts` into {table_name: set(column_names)} so
    membership checks below are cheap and format-agnostic.

    Accepts either:
      - {table_name: {"columns": [{"name": ...}, ...], ...}}  (raw
        format_schema_for_prompt-style dict), or
      - {table_name: AssetContext-shaped dict with "columns": [...]}
    """
    lookup = {}
    for table_name, ctx in asset_contexts.items():
        cols = ctx.get("columns", [])
        lookup[table_name] = {c["name"] for c in cols if "name" in c}
    return lookup


def validate_dimension_output(dq_json: dict, asset_contexts: dict, allowed_dimensions) -> ValidationReport:
    """
    Validates the LLM's dimension-recommendation JSON against the taxonomy
    and against the AssetContext actually sent to the model.

    dq_json: parsed JSON with "tables": [...] and optional "cross_table_checks"
    asset_contexts: {table_name: {..., "columns": [{"name": ...}, ...]}}
    allowed_dimensions: iterable of allowed dimension names (case-insensitive
        match against dq_json's "dimension" field)
    """
    allowed_lower = {d.lower() for d in allowed_dimensions}
    lookup = _asset_lookup(asset_contexts)

    accepted_tables = []
    rejected = []

    for table_entry in dq_json.get("tables", []):
        table = table_entry.get("table")
        if table not in lookup:
            rejected.append({
                "scope": "table",
                "reason": "table not present in AssetContext sent to the LLM",
                "detail": table,
            })
            continue

        known_columns = lookup[table]
        accepted_dims = []

        for dim in table_entry.get("dimensions", []):
            dimension = dim.get("dimension", "")
            columns = dim.get("columns", [])

            if not dimension or not columns:
                rejected.append({
                    "scope": f"{table}",
                    "reason": "missing required field ('dimension' or 'columns')",
                    "detail": dim,
                })
                continue

            if dimension.lower() not in allowed_lower:
                rejected.append({
                    "scope": f"{table}",
                    "reason": "dimension not in the closed taxonomy",
                    "detail": dimension,
                })
                continue

            unknown_cols = [c for c in columns if c not in known_columns]
            if unknown_cols:
                # Drop only the invented column(s), not the whole recommendation:
                # one bad name among several valid ones shouldn't cost the table
                # a dimension.
                kept = [c for c in columns if c in known_columns]
                rejected.append({
                    "scope": f"{table}/{dimension}",
                    "reason": ("column(s) removed -- not present in AssetContext (hallucinated)" if kept
                               else "references column(s) not present in AssetContext (hallucinated)"),
                    "detail": unknown_cols,
                })
                if not kept:
                    continue
                dim = {**dim, "columns": kept}

            accepted_dims.append(dim)

        if accepted_dims:
            accepted_tables.append({"table": table, "dimensions": accepted_dims})

    accepted_cross = []
    for check in dq_json.get("cross_table_checks", []) or []:
        tables_involved = check.get("tables_involved", [])
        columns_involved = check.get("columns_involved", [])
        dimension = check.get("dimension", "")

        missing_tables = [t for t in tables_involved if t not in lookup]
        if missing_tables:
            rejected.append({
                "scope": "cross_table_check",
                "reason": "references table(s) not present in AssetContext",
                "detail": missing_tables,
            })
            continue

        if dimension and dimension.lower() not in allowed_lower:
            rejected.append({
                "scope": "cross_table_check",
                "reason": "dimension not in the closed taxonomy",
                "detail": dimension,
            })
            continue

        # A cross-table column is valid if it exists in at least one of the
        # involved tables (the two tables may not share identical schemas).
        missing_cols = [
            c for c in columns_involved
            if not any(c in lookup[t] for t in tables_involved)
        ]
        if missing_cols:
            kept = [c for c in columns_involved if c not in missing_cols]
            rejected.append({
                "scope": "cross_table_check",
                "reason": ("column(s) removed -- not present in any involved table" if kept
                           else "references column(s) not present in any involved table"),
                "detail": missing_cols,
            })
            if not kept:
                continue
            check = {**check, "columns_involved": kept}

        accepted_cross.append(check)

    accepted_json = {"tables": accepted_tables}
    if accepted_cross:
        accepted_json["cross_table_checks"] = accepted_cross

    return ValidationReport(accepted=accepted_json, rejected=rejected)


# ---------------------------------------------------------------------------
# TestSpecification validation (Test Specifier -> Validator step).
#
# Same "one common validator" principle as above, applied to the second LLM
# output type in the pipeline: TestSpecifications. It checks the same kind of
# things (shape, taxonomy/catalog membership, hallucination against
# AssetContext) plus two rules specific to TestSpecifications:
#   - a threshold may only be set if evidence is cited (structural check --
#     it does NOT judge whether the evidence actually justifies the number,
#     only that something was cited);
#   - every evidence path must resolve to a real, non-"unknown" fact inside
#     the AssetContext that was actually sent to the LLM.
# ---------------------------------------------------------------------------

ALLOWED_THRESHOLD_OPERATORS = {
    "equals", "not_equals",
    "less_than_or_equal", "greater_than_or_equal",
    "less_than", "greater_than",
}


# The row_predicate condition grammar (render_sql.py) uses
# short-form comparison ops (eq/neq/gt/gte/lt/lte); threshold/MetricThreshold
# operators use long-form names instead. Observed behavior: the model mixes
# the two vocabularies up (e.g. writes a MetricThreshold operator as "lte"),
# which used to get silently rejected as "operator not supported" even
# though the intent was completely unambiguous. Normalizing short-form
# aliases to their canonical long form here -- rather than just accepting
# both forms downstream -- means the rest of the pipeline (in particular
# render_sql.py's _COMPARATORS, which only has long-form
# keys) never needs to know the alias existed.
_THRESHOLD_OPERATOR_ALIASES = {
    "eq": "equals", "neq": "not_equals",
    "gt": "greater_than", "gte": "greater_than_or_equal",
    "lt": "less_than", "lte": "less_than_or_equal",
}


def _normalize_threshold_operator(operator):
    return _THRESHOLD_OPERATOR_ALIASES.get(operator, operator)


CROSS_ASSET_ASSERTION_TYPES = {
    "cross_asset_row_count", "cross_asset_missing_rows", "cross_asset_column_equality",
}


# Assertion types that are meaningless without a pass/fail threshold attached.
# A "unique" or "cross_asset_row_count" proposal with no threshold has no
# executable criterion -- it's an observation, not a test. Found empirically:
# such specs reached "accepted" with threshold=None, because the threshold
# block below only validates a threshold's *shape* when one is present -- it
# never required one to exist for these types.
# cross_asset_row_count is NOT here: the Test Specifier prompt asks for it WITHOUT a
# threshold when the tolerance is unknown (a reviewer sets it); it then runs as
# observation-only (`no_threshold`). Rejecting it discarded every such proposal.
REQUIRES_THRESHOLD_ASSERTION_TYPES = {"unique"}


# Flag a "unique" proposal for review (threshold suspended, see _flag_for_review)
# when the profile reports a duplicate rate above this fraction for the same key:
# either the key is wrong for this table or the data has real duplicates, and
# only the reviewer can tell which. Kept low enough that a real candidate key
# (~0% duplicates) clears it easily. Also the limit above which a comparison
# key is not an identifier of the reference rows (rejected: the test is unsound).
UNIQUENESS_DUPLICATE_RATE_THRESHOLD = 0.05


@dataclass
class TestSpecValidationReport:
    accepted: list
    rejected: list = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return len(self.rejected) == 0

    def summary(self) -> str:
        lines = [
            f"Output Contract Validator (TestSpecifications): "
            f"{len(self.accepted)} specification(s) accepted.",
        ]
        if self.rejected:
            lines.append(f"{len(self.rejected)} specification(s) REJECTED:")
            for r in self.rejected:
                lines.append(f"  - [{r['scope']}] {r['reason']} -- {r.get('detail', '')}")
        return "\n".join(lines)


def _find_asset_prefix(asset_contexts: dict, path: str):
    """
    If `path` starts with a known asset name followed by '.', returns
    (matched_asset, remaining_path). Tries longer/more specific asset names
    first so e.g. "staging.x_trans.columns.y" can't get cut short by a
    shorter, unrelated key. Returns (None, path) if no asset name prefixes it.
    """
    for candidate in sorted(asset_contexts.keys(), key=len, reverse=True):
        prefix = candidate + "."
        if path.startswith(prefix):
            return candidate, path[len(prefix):]
    return None, path


def _normalize_evidence_list(evidence) -> list:
    """
    Normalizes a spec's evidence list so every entry is a dict with at least
    a "path" key. The prompt always asks for entries shaped like
    {"path": "..."}, but weaker models
    sometimes emit a bare string instead, e.g. "columns.foo.description"
    instead of {"path": "columns.foo.description"} -- same intent, wrong
    shape. Coerce bare strings into the expected dict shape here, once, so
    every downstream consumer (the bad_evidence checks below,
    _find_uniqueness_contradiction, _attach_observed_evidence) can keep
    assuming dicts without its own defensive isinstance check. Without this,
    a bare-string entry crashes the whole validation pass with "'str' object
    has no attribute 'get'" instead of being rejected/accepted per-spec like
    everything else.
    """
    normalized = []
    for e in evidence or []:
        normalized.append({"path": e} if isinstance(e, str) else e)
    return normalized


def _get_evidence_value(asset_contexts: dict, default_asset: str, path: str):
    """
    Resolves a dot-separated evidence path against the AssetContext it
    actually refers to, and returns (resolved: bool, value). In practice the
    model rarely follows the exact "columns.score.description" /
    "transformation.raw_sql" shape shown in the prompt -- observed behavior
    is that it commonly:
      - Prefixes the path with an asset name, e.g.
        "landing.x.columns.score" instead of "columns.score" -- sometimes
        the spec's OWN asset (pure redundancy), sometimes a DIFFERENT,
        still-legitimate asset (e.g. for a cross-asset check, citing the
        downstream table whose transformation SQL documents the join,
        rather than either of the two tables actually being compared).
      - Stops one field short, e.g. "columns.classification" instead of
        "columns.classification.type", or "transformation" instead of
        "transformation.raw_sql".
    Both get normalized here rather than rejected outright -- this loosens
    the FORMAT the model is allowed to cite, not what counts as a real,
    known fact: it still returns (False, None) whenever the (normalized)
    path doesn't exist or resolves to "unknown"/empty.

    Special-cases "columns.<name>[.<field>]" since columns are stored as a
    list of dicts, not a dict keyed by name; citing "columns.<name>" with no
    field resolves against the column's declared type (always present once
    the column exists).

    Returning the actual value (not just whether it resolves) is what lets
    the accepted spec's evidence entries get enriched with the real
    underlying fact -- see _attach_observed_evidence() below -- so a
    reviewer reading reviews/*.json sees what was actually measured, not
    just a path they'd have to go look up themselves.
    """
    matched_asset, remainder_path = _find_asset_prefix(asset_contexts, path)
    asset = matched_asset if matched_asset else default_asset
    path = remainder_path if matched_asset else path

    ctx = asset_contexts.get(asset)
    if ctx is None or not path:
        return False, None

    parts = [p.strip() for p in path.split(".")]  # tolerate "columns.x. type"
    if parts == ["transformation"]:
        parts = ["transformation", "raw_sql"]

    if parts[0] == "columns" and len(parts) >= 2:
        # Longest name first: nested columns contain dots ("columns.sources.uri.type"
        # is column "sources.uri", field "type" -- not a column called "sources").
        columns_by_name = {c.get("name"): c for c in ctx.get("columns", [])}
        for end in range(len(parts), 1, -1):
            col = columns_by_name.get(".".join(parts[1:end]))
            if col is not None:
                remainder = parts[end:]
                break
        else:
            return False, None
        if not remainder:
            # Citing "the column" with no specific field -- valid as long
            # as it exists with a declared type (always true once found).
            return bool(col.get("type")), col.get("type")
        value = col
        for p in remainder:
            if not isinstance(value, dict) or p not in value:
                return False, None
            value = value[p]
        return value not in (None, "unknown", "", [], {}), value

    value = ctx
    for p in parts:
        if not isinstance(value, dict) or p not in value:
            return False, None
        value = value[p]
    return value not in (None, "unknown", "", [], {}), value


def _resolve_evidence_path(asset_contexts: dict, default_asset: str, path: str) -> bool:
    """Thin bool-only wrapper around _get_evidence_value(), for call sites
    that only need the accept/reject decision, not the resolved value."""
    return _get_evidence_value(asset_contexts, default_asset, path)[0]


def _get_profile_evidence_value(profiling_summary: dict, path: str):
    """
    Resolves a "profile:<check_id>" evidence path against the profiling
    summary (see build_profiling_summary()), and returns (resolved: bool, value). value
    is the entry's "rows" list (the actual observed rows for that check_id)
    when resolved is True, so a reviewer sees the real measured numbers
    behind an evidence citation, not just its path.

    NOTE -- the profile is evidence, not a rule. This function only checks that the cited
    profiling entry EXISTS and has data -- same "structural, not semantic"
    boundary as _get_evidence_value -- it does not judge whether the
    observed values actually justify the threshold.
    """
    if not profiling_summary or not path.startswith("profile:"):
        return False, None
    check_id = path[len("profile:"):]
    entry = profiling_summary.get(check_id)
    rows = entry.get("rows") if entry else None
    return bool(rows), rows


def _resolve_profile_evidence_path(profiling_summary: dict, path: str) -> bool:
    """Thin bool-only wrapper around _get_profile_evidence_value(), for call
    sites that only need the accept/reject decision, not the resolved rows."""
    return _get_profile_evidence_value(profiling_summary, path)[0]


def _find_uniqueness_contradiction(evidence: list, resolve_value, columns=None):
    """
    For a "unique" assertion, checks whether any cited profiling evidence for
    the SAME key the assertion targets already reports a duplicate rate that
    contradicts the claim -- and if so, returns a review warning (the caller
    suspends the threshold, see _flag_for_review), None if there is none.

    This deliberately stays inside the validator's "structural, not
    semantic" boundary (see _get_profile_evidence_value's docstring): a
    "profile:uniqueness:..." evidence row already carries a
    "*_duplicate_count" field alongside "total_rows" for the exact column(s)
    the assertion claims are unique. Comparing those two numbers from the
    SAME cited row is not an inference about relevance or business meaning --
    it's checking whether the assertion contradicts the one piece of
    evidence it chose to cite for itself.

    `columns` is the assertion's own `columns` list, used to pick the ONE
    duplicate-count key that actually corresponds to the claimed key: a
    profiling entry commonly bundles stats for several columns of the same
    table in one row (e.g. an order_id-only assertion's cited path can
    still carry duplicate counts for sibling columns like
    customer_id) -- checking every "*_duplicate_count" key in the
    row, instead of just the claimed one, produced false rejections on
    genuinely-unique keys whose evidence row happened to also describe a
    non-unique sibling column. If columns is empty/None (table-scope
    assertion), every "*_duplicate_count" key in the row is checked instead,
    since there's no narrower key to prefer.

    Found empirically: "unique" TestSpecifications that cited evidence
    showing 80-97% duplicates on the proposed key.
    """
    columns = columns or []
    if len(columns) > 1:
        preferred_keys = {"composite_duplicate_count"}
    elif len(columns) == 1:
        preferred_keys = {f"{metric_alias(columns[0])}_duplicate_count"}
    else:
        preferred_keys = None  # no single claimed key -- fall back to "any"

    for e in evidence:
        path = e.get("path", "")
        if not path.startswith("profile:uniqueness:"):
            continue
        ok, rows = resolve_value(path)
        if not ok or not rows:
            continue
        row = rows[0] if isinstance(rows, list) else rows
        if not isinstance(row, dict):
            continue
        total = row.get("total_rows")
        if not total:
            continue
        candidate_keys = (
            [k for k in preferred_keys if k in row] if preferred_keys is not None
            else [k for k in row if k.endswith("_duplicate_count")]
        )
        for key in candidate_keys:
            dup = row.get(key) or 0
            if not dup:
                continue
            rate = dup / total
            if rate > UNIQUENESS_DUPLICATE_RATE_THRESHOLD:
                return (
                    f"unique assertion contradicted by its own cited evidence: "
                    f"{key}={dup}/{total} ({rate:.1%} duplicates) at {path!r} -- "
                    f"exceeds the {UNIQUENESS_DUPLICATE_RATE_THRESHOLD:.0%} "
                    "threshold for a plausible uniqueness claim"
                )
    return None


# When the observed profile of the SAME columns contradicts a proposed rule. Above these
# limits the rule may be wrong (a filter taken for a domain, an optional column) OR the data
# may have a real, widespread defect -- the validator cannot tell which, so the spec is kept
# with its threshold suspended and a warning for the reviewer (_flag_for_review). Below them,
# a few nulls/duplicates/odd values are a finding, not a bad test. Values chosen on one
# dataset: they decide what gets flagged, never what gets deleted.
IN_LIST_MISSING_PCT = 5.0           # values outside an `in` list covering >= 5% of rows


JOIN_KEY_NULL_RATE = 0.01           # a join key NULL in > 1% of rows (those rows can never match)


OPTIONAL_NULL_RATE = 0.5            # NULL in > 50% of rows (optional by design, or never filled?)


# Not a data judgment: a join key whose non-date part is distinct in < 1% of rows does not
# identify rows, so the test cannot measure missing rows at all (rejected).
KEY_IDENTIFIES_MIN_DISTINCT = 0.01


def _profile_facts(profiling_summary: dict, asset: str) -> dict:
    """Per-column facts for `asset` from the profiling summary (check_ids start with
    '<dimension>:<asset>:'): null counts, duplicate counts, composite duplicates by
    column set, and the observed value frequencies."""
    facts = {"nulls": {}, "dups": {}, "composite": {}, "values": {}, "values_when": {}}
    for check_id, entry in (profiling_summary or {}).items():
        rows = entry.get("rows") or []
        dim = check_id.split(":", 1)[0]
        if not rows or not check_id.startswith(f"{dim}:{asset}:"):
            continue
        ctype = entry.get("check_type") or ""
        cols = [c.strip() for c in str(entry.get("column", "")).split(",") if c.strip()]
        row = rows[0]
        if dim in ("completeness", "join_key_stats") and ctype in ("", "join_key_stats"):
            for c in cols:
                n = row.get(f"{metric_alias(c)}_nulls", row.get("nulls") if len(cols) == 1 else None)
                if n is not None and row.get("total_rows"):
                    facts["nulls"][c] = (n, row["total_rows"])
        elif dim == "uniqueness" and ctype == "":
            for c in cols:
                d = row.get(f"{metric_alias(c)}_duplicate_count")
                if d is not None and row.get("total_rows"):
                    facts["dups"][c] = (d, row["total_rows"])
        elif dim == "uniqueness" and ctype == "composite_key" and row.get("total_rows"):
            facts["composite"][frozenset(cols)] = (row.get("composite_duplicate_count") or 0, row["total_rows"])
        elif dim == "validity" and ctype == "" and len(cols) == 1 and "value" in row:
            observed = [(r.get("value"), r.get("pct_of_total") or 0) for r in rows]
            if "when" in entry:  # values among the rows where a condition holds (for implies(..., col in [...]))
                facts["values_when"][(cols[0], json.dumps(entry["when"], sort_keys=True))] = observed
            else:
                facts["values"][cols[0]] = observed
    return facts


def _norm_value(v) -> str:
    return str(int(v) if isinstance(v, bool) else v).strip().lower()


def _leaf_contradiction(node: dict, facts: dict):
    if node.get("op") == "implies" and (node.get("then") or {}).get("op") == "in" and isinstance(node.get("if"), dict):
        then_col = ((node["then"].get("left") or {}).get("column"))
        observed = facts["values_when"].get((then_col, json.dumps(node["if"], sort_keys=True)))
        if observed:
            allowed = {_norm_value(v) for v in node["then"].get("values") or []}
            outside = [(v, pct) for v, pct in observed if v is not None and _norm_value(v) not in allowed]
            pct = sum(p for _, p in outside)
            if pct >= IN_LIST_MISSING_PCT:
                examples = ", ".join(repr(v) for v, _ in outside[:3])
                return (f"where the `if` holds, {then_col} in {node['then'].get('values')} leaves out {pct:.1f}% of those rows "
                        f"({examples}): the condition classifies rows downstream, it does not restrict {then_col}")
        return None
    left = node.get("left") or {}
    col = left.get("column")
    if not col or "function" in left:
        return None
    if node.get("op") == "is_not_null" and col in facts["nulls"]:
        n, total = facts["nulls"][col]
        if total and n >= total:
            return f"{col} is NULL in every profiled row ({n}/{total}): a not-null check can never pass"
        if total and n / total > OPTIONAL_NULL_RATE:
            return (f"{col} is NULL in {n}/{total} rows ({n / total:.0%}): a column empty in most rows is optional by "
                    "design, so a plain not-null rule would only report that design (restrict it with `implies`)")
    if node.get("op") == "in" and col in facts["values"]:
        allowed = {_norm_value(v) for v in node.get("values") or []}
        outside = [(v, pct) for v, pct in facts["values"][col] if v is not None and _norm_value(v) not in allowed]
        pct = sum(p for _, p in outside)
        if pct >= IN_LIST_MISSING_PCT:
            examples = ", ".join(repr(v) for v, _ in outside[:3])
            return (f"{col} in {node.get('values')} leaves out observed values covering {pct:.1f}% of rows "
                    f"({examples}): the list is a filter used downstream, not the column's domain")
    return None


def _profile_contradictions(spec: dict, profiling_summary: dict):
    """(reject_reason or None, [warnings]) for a spec judged on the profile of its own columns,
    cited or not. Rejected only when the test cannot measure what it claims; a rule the data
    contradicts is a warning for the reviewer (see _flag_for_review)."""
    a = spec.get("assertion") or {}
    a_type = a.get("type")
    warnings = []
    if a_type == "unique":
        facts = _profile_facts(profiling_summary, spec.get("asset"))
        cols = a.get("columns") or []
        d = (facts["dups"].get(cols[0]) if len(cols) == 1 else None) or facts["composite"].get(frozenset(cols))
        if d and d[1] and d[0] / d[1] > UNIQUENESS_DUPLICATE_RATE_THRESHOLD:
            warnings.append(f"the observed profile has {d[0]}/{d[1]} ({d[0] / d[1]:.1%}) duplicates on {cols}")
    elif a_type == "row_predicate":
        facts = _profile_facts(profiling_summary, spec.get("asset"))
        cond = a.get("condition") or {}
        clauses = cond.get("clauses") if cond.get("op") == "and" else [cond]
        warnings += [r for r in (_leaf_contradiction(c, facts) for c in clauses or []) if r]
    elif a_type in ("cross_asset_missing_rows", "cross_asset_column_equality"):
        t1, t2 = a.get("tables") or [None, None]
        f1, f2 = _profile_facts(profiling_summary, t1), _profile_facts(profiling_summary, t2)
        for left, right in join_pairs(a):
            for col, facts, table in ((left, f1, t1), (right, f2, t2)):
                n, total = facts["nulls"].get(col, (0, 0))
                if total and n / total > JOIN_KEY_NULL_RATE:
                    warnings.append(f"join_key column {table}.{col} is NULL in {n}/{total} rows ({n / total:.1%}): "
                                    "a NULL key never matches, so those rows are reported missing")
        if a_type == "cross_asset_column_equality":
            right = [r for _, r in join_pairs(a)]
            d = (f2["dups"].get(right[0]) if len(right) == 1 else None) or f2["composite"].get(frozenset(right))
            if d and d[1] and d[0] / d[1] > UNIQUENESS_DUPLICATE_RATE_THRESHOLD:
                return (f"join_key {right} repeats in {t2} ({d[0]}/{d[1]} duplicate rows, {d[0] / d[1]:.1%}): the key does not "
                        "identify one reference row, so comparing values multiplies rows and the count means nothing"), []
    return None, warnings


_DATE_LITERAL = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$")


def _non_date_literals(cond, types: dict) -> list:
    """Comparisons of a Date/DateTime column with a string that is not a date -- typically a run parameter of the
    SQL template ('EXECUTION_DATE') copied as a value: ClickHouse cannot run it, and it is not a rule on the rows."""
    out = []
    if isinstance(cond, list):
        for x in cond:
            out += _non_date_literals(x, types)
    elif isinstance(cond, dict):
        left, right = cond.get("left") or {}, cond.get("right") or {}
        col = left.get("column") if isinstance(left, dict) and "function" not in left else None
        vals = [right.get("value")] if isinstance(right, dict) and "value" in right else []
        vals += cond.get("values") or [] if cond.get("op") in ("in", "not_in") else []
        if col and "Date" in types.get(col, ""):
            out += [f"{col} ({types[col]}) compared with '{v}', which is not a date (a run parameter is not a value)"
                    for v in vals if isinstance(v, str) and not _DATE_LITERAL.match(v)]
        for k, v in cond.items():
            if k not in ("left", "right") and isinstance(v, (dict, list)):
                out += _non_date_literals(v, types)
    return out


def _sql_facts(asset_contexts: dict, table: str) -> dict:
    """The table's dq_signals plus the SQL facts kept out of the prompts (`_sql_facts`, set when the
    Invariant Extractor is on): the direction checks are logic about the SQL, whatever the model saw."""
    ctx = asset_contexts.get(table) or {}
    return {**(ctx.get("dq_signals") or {}), **(ctx.get("_sql_facts") or {})}


def _column_types(asset_contexts: dict, table: str) -> dict:
    return {c.get("name"): str(c.get("type", "")) for c in (asset_contexts.get(table) or {}).get("columns", [])}


def _flag_for_review(spec: dict, warnings: list) -> None:
    """The observed data contradicts this rule: keep the test but suspend its verdict (the threshold
    moves to `suspended_threshold`) and tell the reviewer why. The rule may be wrong, or the data may
    have a real defect -- deleting the test would hide the second case. Flagged once: a spec that
    already carries review_warnings has been seen, so the reviewer's choice is kept."""
    if not warnings or spec.get("review_warnings"):
        return
    spec["review_warnings"] = warnings
    if spec.get("threshold"):
        spec["suspended_threshold"] = spec.pop("threshold")


def _attach_observed_evidence(evidence: list, resolve_value) -> None:
    """
    Mutates `evidence` (a spec's evidence list) in place, adding an
    "observed" key to each entry holding the actual fact/value the path
    resolved to -- not just the path string. `resolve_value(path)` must
    return (bool, value), e.g. a closure over _get_evidence_value /
    _get_profile_evidence_value with whatever asset(s)/profiling_summary
    are in scope for the caller.

    Only called after the spec has already been accepted (i.e. every path
    is already known to resolve) -- this never changes accept/reject
    outcomes, it only enriches what a human reviewer sees in reviews/*.json
    and the run manifest. If resolve_value ever returns a falsy value for
    an already-accepted path (shouldn't happen), "observed" is simply
    omitted for that entry rather than guessed at.
    """
    for e in evidence:
        ok, value = resolve_value(e.get("path", ""))
        if ok and value is not None:
            e["observed"] = value


def _row_predicate_referenced_columns(node: dict) -> list:
    """
    Recursively walks a row_predicate condition tree (see
    render_sql.py's _render_condition() for the grammar) and
    collects every column name referenced by either side of a leaf
    comparison, so hallucinated columns are caught the same way they were
    for the four types this one replaced (not_null, accepted_range,
    accepted_values, row_rule).
    """
    op = node.get("op")
    cols = []

    if op in ("and", "or"):
        for clause in node.get("clauses") or []:
            cols.extend(_row_predicate_referenced_columns(clause))
        return cols

    if op == "implies":
        for key in ("if", "then"):
            clause = node.get(key)
            if clause:
                cols.extend(_row_predicate_referenced_columns(clause))
        return cols

    if op in ("is_null", "is_not_null", "in", "not_in"):
        left = node.get("left") or {}
        if "column" in left:
            cols.append(left["column"])
        return cols

    # comparison leaf (eq/neq/gt/gte/lt/lte): either side may be a column
    for side_key in ("left", "right"):
        side = node.get(side_key) or {}
        if "column" in side:
            cols.append(side["column"])
    return cols


def _row_predicate_function_errors(node) -> list:
    """Every `function` in a row_predicate condition must come from the renderer's
    closed whitelist, and column functions (length, domain, ...) need a column."""
    errors = []
    if isinstance(node, dict):
        fn = node.get("function")
        if fn is not None:
            if fn in _ROW_PREDICATE_COLUMN_FUNCTIONS:
                if "column" not in node:
                    errors.append(f"{fn}() needs a 'column'")
            elif fn not in _ROW_PREDICATE_FUNCTIONS:
                errors.append(f"unsupported function {fn!r}")
        for value in node.values():
            errors.extend(_row_predicate_function_errors(value))
    elif isinstance(node, list):
        for value in node:
            errors.extend(_row_predicate_function_errors(value))
    return errors


def _referenced_columns(assertion: dict):
    """
    Extracts the column names an assertion references, per assertion type,
    so they can be checked against the AssetContext. Cross-asset types are
    validated separately (they reference tables + an optional join_key
    instead of a single asset's columns).
    """
    a_type = assertion.get("type")
    if a_type == "unique":
        return list(assertion.get("columns", []))
    if a_type == "row_predicate":
        return _row_predicate_referenced_columns(assertion.get("condition", {}))
    return []


# NOT repaired: a missing threshold on unique / cross_asset_row_count. Filling
# "equals 0" was tried and reverted: every repaired test failed -- the missing
# threshold was in practice filtering out uniqueness proposals that were wrong.


def _repair_condition(node) -> bool:
    """In place: {"or": [a, b]} -> {"op": "or", "clauses": [a, b]} at any depth
    (otherwise accepted, then rendered as a TODO and never run). True if changed."""
    if isinstance(node, list):
        return any([_repair_condition(n) for n in node])
    if not isinstance(node, dict):
        return False
    changed = False
    for logical in ("and", "or"):
        if "op" not in node and isinstance(node.get(logical), list):
            node["op"], node["clauses"] = logical, node.pop(logical)
            changed = True
    return any([_repair_condition(v) for v in node.values()]) or changed


def _repair_spec_format(spec) -> None:
    """Repairs two LLM formatting slips that otherwise discard valid tests, and
    records each one in spec["validator_repairs"] so runs can report how many
    tests were repaired: (1) a bare condition placed directly in `assertion`
    (no {"type": "row_predicate", "condition": ...} wrapper); (2) {"or": [...]}
    instead of {"op": "or", "clauses": [...]}."""
    if not isinstance(spec, dict) or not isinstance(spec.get("assertion"), dict):
        return
    assertion = spec["assertion"]
    repairs = []
    if "type" not in assertion and "op" in assertion:
        spec["assertion"] = assertion = {"type": "row_predicate", "condition": assertion}
        repairs.append("wrapped bare condition as row_predicate")
    if assertion.get("type") == "row_predicate" and _repair_condition(assertion.get("condition")):
        repairs.append('rewrote {"and"/"or": [...]} as {"op", "clauses"}')
    if repairs:
        spec["validator_repairs"] = repairs


def validate_test_specifications(
    test_specs, asset_contexts: dict, allowed_assertion_types,
    allow_threshold_without_evidence: bool = False,
    profiling_summary: dict = None,
) -> TestSpecValidationReport:
    """
    Validates a list of TestSpecification dicts (as produced by the Test
    Specifier step) against the AssetContext and the fixed assertion-type
    catalog. Mirrors validate_dimension_output's responsibilities for the
    second stage of the pipeline.

    allow_threshold_without_evidence: normally False, so a fresh LLM
    proposal must cite an AssetContext path to justify any threshold it
    sets. Pass True only when the specs being
    validated have already been through a human review pass -- see
    validate_reviewed_test_specifications() below, which is the intended
    caller for that case rather than passing True directly.

    profiling_summary: optional -- if provided, evidence paths starting with
    "profile:" are also accepted (resolved against this summary instead of
    asset_contexts). Leave it None to accept only AssetContext paths as
    evidence.
    """
    allowed = set(allowed_assertion_types)
    lookup = _asset_lookup(asset_contexts)

    accepted = []
    rejected = []
    seen_ids = set()
    seen_signatures = {}

    for spec in test_specs:
        _repair_spec_format(spec)
        spec_id = spec.get("id")
        asset = spec.get("asset")
        assertion = spec.get("assertion", {})
        a_type = assertion.get("type")

        if not spec_id or not asset or not a_type:
            rejected.append({
                "scope": spec_id or "(missing id)",
                "reason": "missing required field ('id', 'asset', or 'assertion.type')",
                "detail": spec,
            })
            continue

        if spec_id in seen_ids:
            rejected.append({
                "scope": spec_id,
                "reason": "duplicate specification id in this batch",
                "detail": asset,
            })
            continue

        if asset not in lookup:
            rejected.append({
                "scope": spec_id,
                "reason": "asset not present in AssetContext sent to the LLM",
                "detail": asset,
            })
            continue

        if a_type not in allowed:
            rejected.append({
                "scope": spec_id,
                "reason": "assertion.type not in the supported catalog",
                "detail": a_type,
            })
            continue

        evidence_asset = asset
        if a_type in CROSS_ASSET_ASSERTION_TYPES:
            tables = assertion.get("tables", [])
            missing_tables = [t for t in tables if t not in lookup]
            if len(tables) != 2 or missing_tables:
                rejected.append({
                    "scope": spec_id,
                    "reason": "cross-asset assertion requires exactly 2 tables, both present in AssetContext",
                    "detail": tables,
                })
                continue
            join_key = assertion.get("join_key")
            try:
                pairs = join_pairs(assertion)
                if not all(isinstance(c, str) for pair in pairs for c in pair):
                    pairs = None  # malformed (e.g. over-nested) join_key -> reject, don't crash
            except (TypeError, IndexError):
                pairs = None
            fits = lambda ps, a, b: all(l in lookup[a] and r in lookup[b] for l, r in ps)
            if pairs and not fits(pairs, tables[0], tables[1]) and fits(pairs, tables[1], tables[0]):
                # pair written for the other table order -> swap each pair (format slip, not content)
                pairs = [(r, l) for l, r in pairs]
                assertion["join_key"] = [[l, r] for l, r in pairs]
                spec.setdefault("validator_repairs", []).append("swapped join_key pairs to match the order of `tables`")
            if pairs is None or any(
                left not in lookup[tables[0]] or right not in lookup[tables[1]] for left, right in pairs
            ):
                rejected.append({
                    "scope": spec_id,
                    "reason": "join_key not present as a column in both involved tables",
                    "detail": join_key,
                })
                continue
            if not isinstance(assertion.get("filters") or {}, dict):  # checked before any use below
                rejected.append({"scope": spec_id, "reason": "invalid cross-asset filters",
                                 "detail": ["'filters' must be {table: condition}"]})
                continue
            # source -> target with no filter on a source the target's SQL reads FILTERED: the source rows the
            # filter drops are legitimately absent, so this direction reports them all as "missing"
            src_filter = next((s for k, s in _sql_facts(asset_contexts, tables[1]).items()
                               if k.startswith("source_filter_") and s.get("upstream") == tables[0]), None)
            if a_type == "cross_asset_missing_rows" and src_filter and not (assertion.get("filters") or {}).get(tables[0]):
                rejected.append({
                    "scope": spec_id,
                    "reason": f"{tables[1]} reads {tables[0]} only where {src_filter.get('condition', '')[:150]}: rows outside "
                              f"that filter are legitimately absent -- use tables [\"{tables[1]}\", \"{tables[0]}\"] "
                              f"(every {tables[1]} row must come from {tables[0]}) or filter {tables[0]} the same way",
                    "detail": join_key,
                })
                continue
            # target -> one source of a UNION ALL target, unfiltered: the other branches' rows can never match
            union = _sql_facts(asset_contexts, tables[0]).get("union_of_sources") or {}
            if a_type == "cross_asset_missing_rows" and tables[1] in (union.get("tables") or []) \
                    and not (assertion.get("filters") or {}).get(tables[0]):
                rejected.append({
                    "scope": spec_id,
                    "reason": f"{tables[0]} is the UNION ALL of {', '.join(union['tables'])}: only part of its rows come "
                              f"from {tables[1]}, the rest would all be reported missing -- filter {tables[0]} to that "
                              f"branch, or check the other direction when {tables[1]} is read unfiltered",
                    "detail": join_key,
                })
                continue
            types = {t: {c.get("name"): str(c.get("type", "")) for c in asset_contexts.get(t, {}).get("columns", [])} for t in tables}
            rest = [l for l, r in pairs if "Date" not in types[tables[0]].get(l, "")] if pairs else []
            rest_dups = _profile_facts(profiling_summary, tables[0])["composite"].get(frozenset(rest)) if rest and len(rest) < len(pairs) else None
            only_dates_identify = bool(rest_dups and rest_dups[1] and (rest_dups[1] - rest_dups[0]) / rest_dups[1] < KEY_IDENTIFIES_MIN_DISTINCT)
            if pairs and (only_dates_identify or
                          all("Date" in types[tables[0]].get(l, "") and "Date" in types[tables[1]].get(r, "") for l, r in pairs)):
                # many rows share a date/timestamp: such a key matches unrelated rows (and misses renamed ones)
                rejected.append({
                    "scope": spec_id,
                    "reason": "join_key made only of date/time columns: a timestamp does not identify a row -- "
                              "join on identifiers and keep dates, if needed, as an extra key column",
                    "detail": join_key,
                })
                continue
            if a_type == "cross_asset_missing_rows" and not pairs:
                rejected.append({
                    "scope": spec_id,
                    "reason": "cross_asset_missing_rows requires a join_key",
                    "detail": assertion,
                })
                continue
            filters = assertion.get("filters") or {}
            filter_problems = [] if isinstance(filters, dict) else ["'filters' must be {table: condition}"]
            for f_table, condition in (filters.items() if isinstance(filters, dict) else []):
                if f_table not in tables:
                    filter_problems.append(f"filter on a table not in 'tables': {f_table}")
                    continue
                if not isinstance(condition, dict):
                    filter_problems.append(f"{f_table}: the filter must be a row_predicate condition tree, not SQL text")
                    continue
                filter_problems += [f"{f_table}: unknown column {c}" for c in
                                    _row_predicate_referenced_columns(condition or {}) if c not in lookup[f_table]]
                filter_problems += [f"{f_table}: {e}" for e in _row_predicate_function_errors(condition)]
                filter_problems += [f"{f_table}: {e}" for e in _non_date_literals(condition, _column_types(asset_contexts, f_table))]
            if filter_problems:
                rejected.append({
                    "scope": spec_id,
                    "reason": "invalid cross-asset filters",
                    "detail": filter_problems,
                })
                continue
            if a_type == "cross_asset_column_equality":
                if not pairs:
                    rejected.append({
                        "scope": spec_id,
                        "reason": "cross_asset_column_equality requires a join_key "
                                  "(there is no other way to pair rows across the two tables)",
                        "detail": assertion,
                    })
                    continue
                eq_columns = assertion.get("columns")
                if (not isinstance(eq_columns, list) or len(eq_columns) != 2
                        or not all(isinstance(c, str) for c in eq_columns)):
                    rejected.append({
                        "scope": spec_id,
                        "reason": "cross_asset_column_equality requires 'columns': [col_in_table1, "
                                  "col_in_table2]",
                        "detail": eq_columns,
                    })
                    continue
                col1, col2 = eq_columns
                if col1 not in lookup[tables[0]] or col2 not in lookup[tables[1]]:
                    rejected.append({
                        "scope": spec_id,
                        "reason": "cross_asset_column_equality references column(s) not present in "
                                  "AssetContext (hallucinated)",
                        "detail": eq_columns,
                    })
                    continue
            evidence_asset = tables[0]
        else:
            known_columns = lookup[asset]
            unknown_cols = [c for c in _referenced_columns(assertion) if c not in known_columns]
            if unknown_cols:
                rejected.append({
                    "scope": spec_id,
                    "reason": "assertion references column(s) not present in AssetContext (hallucinated)",
                    "detail": unknown_cols,
                })
                continue
            function_errors = _row_predicate_function_errors(assertion.get("condition")) + (
                _non_date_literals(assertion.get("condition"), _column_types(asset_contexts, asset)) if a_type == "row_predicate" else [])
            if a_type == "row_predicate" and function_errors:
                rejected.append({
                    "scope": spec_id,
                    "reason": "row_predicate uses a function outside the supported set, or compares a date column with a non-date value",
                    "detail": function_errors,
                })
                continue

        threshold = spec.get("threshold")
        evidence = spec["evidence"] = _normalize_evidence_list(spec.get("evidence", []))

        if not threshold and a_type in REQUIRES_THRESHOLD_ASSERTION_TYPES and not spec.get("review_warnings"):
            rejected.append({
                "scope": spec_id,
                "reason": f"assertion.type {a_type!r} has no executable pass/fail "
                          "criterion without a threshold (metric/operator/value); "
                          "none was provided",
                "detail": assertion,
            })
            continue

        if threshold:
            required_keys = {"metric", "operator", "value"}
            if not required_keys.issubset(threshold.keys()):
                rejected.append({
                    "scope": spec_id,
                    "reason": "threshold missing 'metric', 'operator', or 'value'",
                    "detail": threshold,
                })
                continue
            threshold["operator"] = _normalize_threshold_operator(threshold["operator"])
            if threshold["operator"] not in ALLOWED_THRESHOLD_OPERATORS:
                rejected.append({
                    "scope": spec_id,
                    "reason": "threshold.operator not supported",
                    "detail": threshold["operator"],
                })
                continue
            if not evidence and not allow_threshold_without_evidence:
                rejected.append({
                    "scope": spec_id,
                    "reason": "threshold set without citing evidence (a threshold may only "
                              "come from an explicit, cited rule)",
                    "detail": threshold,
                })
                continue

        def _evidence_path_value(path: str):
            if path.startswith("profile:"):
                return _get_profile_evidence_value(profiling_summary, path)
            if a_type in CROSS_ASSET_ASSERTION_TYPES:
                # A bare (unprefixed) path is ambiguous between the two
                # tables in a cross-asset check -- e.g. "lineage.upstream_
                # assets" genuinely lives on whichever side is documented as
                # downstream, not necessarily tables[0] (which is often the
                # upstream/source table, whose own lineage is "unknown" by
                # definition). Accept it if it resolves against EITHER
                # table, not just the default evidence_asset.
                for t in assertion["tables"]:
                    ok, value = _get_evidence_value(asset_contexts, t, path)
                    if ok:
                        return ok, value
                return False, None
            return _get_evidence_value(asset_contexts, evidence_asset, path)

        bad_evidence = [e.get("path") for e in evidence if not _evidence_path_value(e.get("path", ""))[0]]
        if bad_evidence:
            rejected.append({
                "scope": spec_id,
                "reason": "evidence path does not resolve to a known fact in AssetContext "
                          "or the profiling summary",
                "detail": bad_evidence,
            })
            continue

        warnings = []
        if a_type == "unique":
            contradiction = _find_uniqueness_contradiction(
                evidence, _evidence_path_value, columns=assertion.get("columns"),
            )
            if contradiction:
                warnings.append(contradiction)

        # Checked against the observed profile of the SAME columns even when the spec does not
        # cite it: a test that cannot measure what it claims is rejected; a rule the data
        # contradicts goes to the reviewer with its verdict suspended (see _flag_for_review).
        if profiling_summary:
            reject_reason, found = _profile_contradictions(spec, profiling_summary)
            if reject_reason:
                rejected.append({"scope": spec_id, "reason": reject_reason, "detail": assertion})
                continue
            warnings += found
        _flag_for_review(spec, warnings)

        signature = json.dumps([spec.get("asset"), spec.get("assertion"), spec.get("threshold")], sort_keys=True, default=str)
        if signature in seen_signatures:
            rejected.append({"scope": spec_id, "reason": f"duplicate of specification {seen_signatures[signature]}",
                             "detail": assertion})
            continue

        # Enrich each evidence entry with what it actually resolved to, so a
        # reviewer reading reviews/*.json sees the real measured fact, not
        # just the path -- see _attach_observed_evidence()'s docstring.
        _attach_observed_evidence(evidence, _evidence_path_value)

        # Accepting a spec the SQL generator can't render means it silently never runs.
        try:
            ASSERTION_RENDERERS[a_type](spec)
        except (KeyError, ValueError, TypeError, IndexError) as e:
            rejected.append({
                "scope": spec_id,
                "reason": "specification cannot be rendered to SQL",
                "detail": str(e),
            })
            continue

        seen_ids.add(spec_id)
        seen_signatures[signature] = spec_id
        accepted.append(spec)

    return TestSpecValidationReport(accepted=accepted, rejected=rejected)


def validate_reviewed_test_specifications(
    test_specs, asset_contexts: dict, allowed_assertion_types, profiling_summary: dict = None,
) -> TestSpecValidationReport:
    """
    Validates a batch of TestSpecifications that have already been through a
    human review pass (see review_test_specifications()
    step). Two differences from validate_test_specifications():

      - A threshold no longer needs a cited evidence path: a person directly
        setting or confirming a threshold in review IS the authority --
        there's no further AssetContext fact to point at.
      - Any specification the reviewer marked `"status": "excluded"` is
        dropped from `accepted` and recorded in `rejected` with that reason,
        instead of being rendered/executed.

    Everything else -- asset/column/table existence, assertion-type
    allowlist, threshold format -- is still checked exactly as before.
    Review relaxes the evidence-for-threshold rule; it does not bypass
    hallucination or shape checks.
    """
    excluded = [s for s in test_specs if s.get("status") == "excluded"]
    kept = [s for s in test_specs if s.get("status") != "excluded"]

    report = validate_test_specifications(
        kept, asset_contexts, allowed_assertion_types,
        allow_threshold_without_evidence=True, profiling_summary=profiling_summary,
    )
    for spec in excluded:
        report.rejected.append({
            "scope": spec.get("id", "(missing id)"),
            "reason": "excluded during specialist review",
            "detail": spec.get("asset"),
        })
    return report


# ---------------------------------------------------------------------------
# SQL guarantee (invariant) validation -- output of the Invariant Extractor.
# The categories are what each SQL operator can guarantee (filter/join, plain
# copy, CASE/IN, COALESCE/IS NOT NULL, GROUP BY/DISTINCT, UNION ALL, computed
# column): the same for any pipeline, never derived from a test set.
# ---------------------------------------------------------------------------
INVARIANT_CATEGORIES = ("row_provenance", "row_completeness", "value_domain", "presence", "grain",
                        "branch_source", "derivation")


# what the SQL needs from a table it READS: values it compares, functions it applies, columns that must be present
INPUT_CATEGORIES = ("input_values", "input_function", "input_presence")


_MIN_QUOTE_CHARS = 8  # a quote shorter than this ("where", "id") grounds nothing


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").lower().replace("`", ""))


def validate_invariants(raw: list, target: str, asset_contexts: dict):
    """(accepted, rejected) for the guarantees the LLM extracted from `target`'s own SQL. Accepted only
    when the category is known, the first table is the target and the others are tables it reads (for an
    input_* requirement: exactly one table, one the target reads), every column exists in those tables, and
    the quoted SQL excerpt really is in the transformation (whitespace, case and backticks ignored) -- the
    check that stops invented guarantees. Ids are reassigned so they are unique and dot-free (evidence path
    `sql_invariants.<id>`)."""
    ctx = asset_contexts.get(target) or {}
    sql = _squash((ctx.get("transformation") or {}).get("raw_sql"))
    upstream = (ctx.get("lineage") or {}).get("upstream_assets")
    upstream = set(upstream) if isinstance(upstream, list) else set()
    accepted, rejected, seen = [], [], set()
    for inv in raw if isinstance(raw, list) else []:
        if not isinstance(inv, dict):
            rejected.append({"scope": target, "reason": "not an object", "detail": inv})
            continue
        tables = [t for t in inv.get("tables") or [] if isinstance(t, str)] or [target]
        is_input = inv.get("category") in INPUT_CATEGORIES
        if target in tables and not is_input:  # format slip: the target listed second -- put it first
            tables = [target] + [t for t in tables if t != target]
        known = {c.get("name") for t in tables for c in (asset_contexts.get(t) or {}).get("columns", [])}
        by_lower = {str(k).lower(): k for k in known}  # format slip: a column name with the wrong case
        fix = lambda c: c if c in known else by_lower.get(c.lower(), c)
        cols = [fix(c) for c in inv.get("columns") or [] if isinstance(c, str)]
        inv = {**inv, "column_pairs": [[fix(a), fix(b)] for a, b in (p for p in inv.get("column_pairs") or []
                                                                  if isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p))]}
        quote = _squash(inv.get("sql_quote"))
        reason = (
            "unknown category" if inv.get("category") not in INVARIANT_CATEGORIES + INPUT_CATEGORIES else
            "an input requirement names exactly one table the transformation reads" if is_input and (
                len(tables) != 1 or tables[0] not in upstream) else
            "first table is not the transformation's output" if not is_input and tables[0] != target else
            "more than two tables" if len(tables) > 2 else
            "table not read by this transformation" if not set(tables[1:]) <= upstream else
            f"unknown column(s) {sorted(set(cols) - known)}" if not set(cols) <= known else
            "quoted SQL excerpt missing or too short" if len(quote) < _MIN_QUOTE_CHARS else
            "quoted SQL excerpt not found in the transformation" if quote not in sql else None
        )
        signature = json.dumps([inv.get("category"), tables, sorted(cols), quote])
        if not reason and signature in seen:
            reason = "duplicate guarantee"
        if reason:
            rejected.append({"scope": target, "reason": reason, "detail": inv})
            continue
        seen.add(signature)
        # [output column, input column] pairs that identify the same row on both sides (a join key, renames
        # included): kept only when each column exists on its own side -- a wrong pair is dropped, not the guarantee
        # never a date/time column (many rows share a timestamp). A Nullable TYPE is not NULL data: whether a key
        # really has NULLs is measured on the data by the spec validation (JOIN_KEY_NULL_RATE)
        key_cols = lambda t: {c.get("name") for c in (asset_contexts.get(t) or {}).get("columns", [])
                              if "Date" not in str(c.get("type", ""))}
        out_cols, in_cols = key_cols(target), key_cols(tables[-1])
        pairs = [p for p in inv.get("column_pairs") or [] if len(tables) == 2 and isinstance(p, list) and len(p) == 2
                 and p[0] in out_cols and p[1] in in_cols]
        accepted.append({**inv, "id": f"{re.sub(r'[^0-9A-Za-z]+', '_', target)}_{len(accepted) + 1}",
                         "tables": tables, "columns": cols, "column_pairs": pairs})
    return accepted, rejected


# ---------------------------------------------------------------------------
# MetricThreshold validation.
#
# A MetricThreshold is a second, lighter-weight kind of threshold the Test
# Specifier can propose, alongside a full TestSpecification. It targets a
# metric that ALREADY EXISTS in this run's profiling output directly (e.g.
# validity's distinct_ratio_pct, accuracy's outliers_3stddev, freshness's
# hours_since_last_update, consistency's functional_dependency violation
# counts) rather than being rendered into its own SQL against the source
# table. This exists specifically for profiling metrics that don't fit any
# of the 8 fixed TestSpecification assertion types -- without it, those
# metrics could be computed and displayed but never actually turned into a
# pass/fail check.
#
# Shape: {id, check_id, metric, asset, title, operator, value, evidence,
#         rationale, status}. Unlike a TestSpecification's `threshold`
# (nested, optional), a MetricThreshold's operator/value ARE the whole
# point -- there is no non-threshold form of it, so both are required.
#
# NOTE -- this lets the LLM set a threshold whose evidence is a single data snapshot,
# not just a documented rule. Same traceability discipline applies: still
# rejects anything that doesn't cite a real, resolvable fact -- it just
# broadens WHAT can be thresholded, not whether evidence is still required.
# ---------------------------------------------------------------------------

def _metric_exists_in_profile(profiling_summary: dict, check_id: str, metric: str) -> bool:
    if not profiling_summary:
        return False
    entry = profiling_summary.get(check_id)
    if not entry or not entry.get("rows"):
        return False
    return any(metric in row for row in entry["rows"])


def validate_metric_thresholds(
    metric_thresholds, asset_contexts: dict, profiling_summary: dict,
    allow_threshold_without_evidence: bool = False,
) -> TestSpecValidationReport:
    """
    Validates a list of MetricThreshold dicts against the profiling summary
    they must reference (a MetricThreshold with no profiling data available
    is meaningless -- there's nothing for it to check) and, for any
    AssetContext-path evidence, against asset_contexts as usual.
    """
    lookup = _asset_lookup(asset_contexts)
    accepted = []
    rejected = []
    seen_ids = set()

    for spec in metric_thresholds:
        spec_id = spec.get("id")
        asset = spec.get("asset")
        check_id = spec.get("check_id")
        metric = spec.get("metric")
        operator = spec.get("operator")
        value = spec.get("value")

        if not spec_id or not asset or not check_id or not metric or operator is None or value is None:
            rejected.append({
                "scope": spec_id or "(missing id)",
                "reason": "missing required field ('id', 'asset', 'check_id', 'metric', "
                          "'operator', or 'value')",
                "detail": spec,
            })
            continue

        if spec_id in seen_ids:
            rejected.append({
                "scope": spec_id, "reason": "duplicate specification id in this batch",
                "detail": asset,
            })
            continue

        if asset not in lookup:
            rejected.append({
                "scope": spec_id,
                "reason": "asset not present in AssetContext sent to the LLM",
                "detail": asset,
            })
            continue

        operator = _normalize_threshold_operator(operator)
        spec["operator"] = operator
        if operator not in ALLOWED_THRESHOLD_OPERATORS:
            rejected.append({
                "scope": spec_id, "reason": "operator not supported", "detail": operator,
            })
            continue

        if not _metric_exists_in_profile(profiling_summary, check_id, metric):
            rejected.append({
                "scope": spec_id,
                "reason": "check_id/metric does not resolve to a real entry in this run's "
                          "profiling summary -- a MetricThreshold must target a metric that "
                          "was actually computed",
                "detail": f"{check_id} -> {metric}",
            })
            continue

        evidence = spec["evidence"] = _normalize_evidence_list(spec.get("evidence", []))
        if not evidence and not allow_threshold_without_evidence:
            rejected.append({
                "scope": spec_id,
                "reason": "MetricThreshold set without citing evidence",
                "detail": {"operator": operator, "value": value},
            })
            continue

        def _evidence_path_value(path: str):
            if path.startswith("profile:"):
                return _get_profile_evidence_value(profiling_summary, path)
            return _get_evidence_value(asset_contexts, asset, path)

        bad_evidence = [e.get("path") for e in evidence if not _evidence_path_value(e.get("path", ""))[0]]
        if bad_evidence:
            rejected.append({
                "scope": spec_id,
                "reason": "evidence path does not resolve to a known fact in AssetContext "
                          "or the profiling summary",
                "detail": bad_evidence,
            })
            continue

        _attach_observed_evidence(evidence, _evidence_path_value)

        seen_ids.add(spec_id)
        accepted.append(spec)

    return TestSpecValidationReport(accepted=accepted, rejected=rejected)


def validate_reviewed_metric_thresholds(
    metric_thresholds, asset_contexts: dict, profiling_summary: dict,
) -> TestSpecValidationReport:
    """Post-review counterpart to validate_metric_thresholds(), mirroring
    validate_reviewed_test_specifications(): drops status=="excluded"
    entries, and no longer requires a cited evidence path (a human directly
    setting/confirming the value in review IS the authority)."""
    excluded = [s for s in metric_thresholds if s.get("status") == "excluded"]
    kept = [s for s in metric_thresholds if s.get("status") != "excluded"]

    report = validate_metric_thresholds(
        kept, asset_contexts, profiling_summary, allow_threshold_without_evidence=True,
    )
    for spec in excluded:
        report.rejected.append({
            "scope": spec.get("id", "(missing id)"),
            "reason": "excluded during specialist review",
            "detail": spec.get("asset"),
        })
    return report
