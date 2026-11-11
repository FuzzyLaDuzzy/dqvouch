"""Renders validated TestSpecifications into deterministic pass/fail SQL (ClickHouse dialect) and describes them.
The LLM only fills typed fields of a closed assertion catalog; every SQL string is built here."""
import re


_COMPARATORS = {
    "equals": lambda obs, thr: obs == thr,
    "not_equals": lambda obs, thr: obs != thr,
    "less_than_or_equal": lambda obs, thr: obs <= thr,
    "greater_than_or_equal": lambda obs, thr: obs >= thr,
    "less_than": lambda obs, thr: obs < thr,
    "greater_than": lambda obs, thr: obs > thr,
}


def _sql_literal(value) -> str:
    """Renders a Python literal (from a validated spec, never raw LLM SQL
    text) as a safe ClickHouse SQL literal."""
    if isinstance(value, bool):
        return "true" if value else "false"  # PostgreSQL has no boolean = integer; ClickHouse reads both
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _metric_name(spec: dict, default: str) -> str:
    """The threshold's metric becomes a column alias, so only a plain identifier is used."""
    metric = (spec.get("threshold") or {}).get("metric", default)
    return metric if isinstance(metric, str) and re.fullmatch(r"[A-Za-z_]\w*", metric) else default


def one_line(text) -> str:
    """Model-written text placed in a `--` SQL comment: a line break would end the comment."""
    return " ".join(str(text).splitlines())


# ---------------------------------------------------------------------------
# Per-assertion-type renderers. Each takes a validated TestSpecification and
# returns a single executable SQL statement (no trailing CHECK_ID/comments --
# those get added once, uniformly, in generate_sql_from_test_specifications).
# ---------------------------------------------------------------------------

def render_unique(spec):
    table = spec["asset"]
    columns = spec["assertion"]["columns"]
    metric = _metric_name(spec, "failed_rows")
    expr = columns[0] if len(columns) == 1 else "(" + ", ".join(columns) + ")"
    return f"SELECT count() - uniqExact({expr}) AS {metric} FROM {table};"


_ROW_PREDICATE_COMPARISON_OPS = {"eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


_ROW_PREDICATE_FUNCTIONS = {
    # Dynamic right-hand-side values, resolved at test-execution time rather
    # than baked in as a literal -- added specifically so "must not be in
    # the future" / "must not be later than now" rules can be expressed as a
    # real per-row SQL comparison (e.g. a single `lte` leaf against `now`)
    # instead of always falling back to a MetricThreshold read of a stale
    # profiling-time snapshot. Deliberately a small, closed set (like every
    # other vocabulary in this module) -- "now" for DateTime columns,
    # "today" for Date columns; add here (and only here) if a genuine new
    # need shows up, never let the LLM emit arbitrary SQL functions.
    "now": "now()",
    "today": "today()",
}


# Functions applied to ONE column of the row -- {"function": "length", "column": "x"}.
# Same closed-whitelist rule as above, and just as generic: any table with arrays,
# URLs or text can use them (e.g. two arrays describing the same items must have
# the same length; a URL must yield a non-empty domain).
_ROW_PREDICATE_COLUMN_FUNCTIONS = {
    "length": "length",   # number of elements of an array (or characters of a string)
    "domain": "domain",   # host of a URL: 'https://cdn.x.com/app.js' -> 'cdn.x.com'
    "trim": "trimBoth",   # text without leading/trailing spaces
    "lower": "lower",     # lower-cased text
}


def _row_predicate_side_sql(side: dict) -> str:
    # "function" first: a column function side also carries a "column" key.
    if "function" in side:
        fn = side["function"]
        if fn in _ROW_PREDICATE_COLUMN_FUNCTIONS:
            if "column" not in side:
                raise KeyError(f"row_predicate function {fn!r} needs a 'column'")
            return f"{_ROW_PREDICATE_COLUMN_FUNCTIONS[fn]}({side['column']})"
        if fn not in _ROW_PREDICATE_FUNCTIONS:
            raise KeyError(f"unsupported row_predicate side function: {fn!r}")
        return _ROW_PREDICATE_FUNCTIONS[fn]
    if "column" in side:
        return side["column"]
    if "value" in side:
        return _sql_literal(side["value"])
    raise KeyError("row_predicate condition side must have 'column', 'value', or 'function'")


def _render_condition(node: dict, depth: int = 0, max_depth: int = 6) -> str:
    """
    Recursively renders a row_predicate condition tree into SQL that is TRUE
    when the rule HOLDS. render_row_predicate() negates the result once, at
    the top, to count violations -- so every node type below describes what
    should be true, not what constitutes a failure.

    Grammar (each node is one of):
      {"op": "and" | "or", "clauses": [node, node, ...]}   -- >= 2 clauses
      {"op": "implies", "if": node, "then": node}          -- NOT(if) OR then
      {"op": "is_null" | "is_not_null", "left": side}
      {"op": "in" | "not_in", "left": side, "values": [literal, ...]}
      {"op": "eq"|"neq"|"gt"|"gte"|"lt"|"lte", "left": side, "right": side}
    where `side` is {"column": str}, {"value": literal}, or
    {"function": "now"|"today"} -- so a comparison may be column-vs-column
    (e.g. end_date >= start_date), column-vs-literal, or column-vs-the
    current moment at test-execution time (e.g. a single `lte` leaf of
    {"left": {"column": "some_timestamp"}, "right": {"function": "now"}}
    for a "must not be in the future" rule on a DateTime column -- use
    "today" instead of "now" for a Date column). See
    _ROW_PREDICATE_FUNCTIONS above for the closed set of supported
    functions.

    Relies on real SQL's 3-valued logic (a comparison against NULL is
    UNKNOWN, not FALSE) to get the same "NULL is not a violation of a range/
    domain check" behavior the old accepted_range/accepted_values renderers
    special-cased explicitly -- countIf() only counts rows where the negated
    expression evaluates to exactly TRUE, so an UNKNOWN outcome is correctly
    never counted as a violation. Verified against the four renderers this
    type replaces (not_null, accepted_range, accepted_values, row_rule) over
    5000 randomized rows each with 0 mismatches before this was integrated.
    """
    if depth > max_depth:
        raise ValueError(f"row_predicate condition exceeds max nesting depth ({max_depth})")

    op = node.get("op")

    if op in ("and", "or"):
        clauses = node.get("clauses") or []
        if len(clauses) < 2:
            raise KeyError(f"row_predicate '{op}' requires at least 2 'clauses'")
        joiner = " AND " if op == "and" else " OR "
        parts = [f"({_render_condition(c, depth + 1, max_depth)})" for c in clauses]
        return joiner.join(parts)

    if op == "implies":
        if_sql = _render_condition(node["if"], depth + 1, max_depth)
        then_sql = _render_condition(node["then"], depth + 1, max_depth)
        return f"NOT ({if_sql}) OR ({then_sql})"

    if op in ("is_null", "is_not_null"):
        left = _row_predicate_side_sql(node["left"])
        return f"{left} IS {'NOT ' if op == 'is_not_null' else ''}NULL"

    if op in ("in", "not_in"):
        left = _row_predicate_side_sql(node["left"])
        values = node.get("values")
        if not values:
            raise KeyError(f"row_predicate '{op}' requires a non-empty 'values' list")
        values_sql = ", ".join(_sql_literal(v) for v in values)
        keyword = "NOT IN" if op == "not_in" else "IN"
        return f"{left} {keyword} ({values_sql})"

    if op in _ROW_PREDICATE_COMPARISON_OPS:
        left = _row_predicate_side_sql(node["left"])
        right = _row_predicate_side_sql(node["right"])
        return f"{left} {_ROW_PREDICATE_COMPARISON_OPS[op]} {right}"

    raise KeyError(f"unsupported row_predicate condition operator: {op!r}")


def render_row_predicate(spec):
    table = spec["asset"]
    condition = spec["assertion"]["condition"]
    metric = _metric_name(spec, "failed_rows")
    holds_sql = _render_condition(condition)
    return f"SELECT countIf(NOT ({holds_sql})) AS {metric} FROM {table};"


def join_pairs(assertion: dict) -> list:
    """Normalizes a cross-asset `join_key` into [(column_in_table1, column_in_table2), ...].
    Accepts a single column ("k"), a composite key (["k1", "k2"]), or pairs for keys
    named differently in the two tables ([["k_in_t1", "k_in_t2"], ...])."""
    key = assertion.get("join_key")
    if not key:
        return []
    if isinstance(key, str):
        return [(key, key)]
    return [(k, k) if isinstance(k, str) else (k[0], k[1]) for k in key]


def _source_sql(assertion: dict, table: str) -> str:
    """The table, restricted to the rows its optional `filters` condition selects
    (filters: {table_name: row_predicate condition}) -- e.g. one category of rows,
    or one partition."""
    condition = (assertion.get("filters") or {}).get(table)
    return f"(SELECT * FROM {table} WHERE {_render_condition(condition)})" if condition else table


def _on_clause(assertion: dict) -> str:
    return " AND ".join(f"a.{left} = b.{right}" for left, right in join_pairs(assertion))


def render_cross_asset_row_count(spec):
    a = spec["assertion"]
    t1, t2 = a["tables"]
    metric = _metric_name(spec, "row_count_diff")
    return (
        f"SELECT (SELECT count() FROM {_source_sql(a, t1)}) - "
        f"(SELECT count() FROM {_source_sql(a, t2)}) AS {metric};"
    )


def render_cross_asset_missing_rows(spec):
    # LEFT ANTI JOIN rather than "LEFT JOIN ... WHERE b.key IS NULL": with ClickHouse's
    # default join_use_nulls = 0, unmatched rows get the key type's default value
    # ('' / 0), not NULL, so the IS NULL form undercounts for non-Nullable keys.
    a = spec["assertion"]
    t1, t2 = a["tables"]
    metric = _metric_name(spec, "missing_count")
    return (
        f"SELECT count() AS {metric}\n"
        f"FROM {_source_sql(a, t1)} AS a\n"
        f"LEFT ANTI JOIN {_source_sql(a, t2)} AS b ON {_on_clause(a)};"
    )


def render_cross_asset_column_equality(spec):
    """Counts, among rows present in both tables (INNER JOIN on join_key --
    existence itself is cross_asset_missing_rows's job, not this one's), how
    many have disagreeing values in the two named columns. Null-safe: both
    sides null counts as equal (not a mismatch); exactly one side null counts
    as a mismatch, same as a plain value disagreement."""
    a = spec["assertion"]
    t1, t2 = a["tables"]
    col1, col2 = a["columns"]
    metric = _metric_name(spec, "mismatched_count")
    return (
        f"SELECT count() AS {metric}\n"
        f"FROM {_source_sql(a, t1)} AS a\n"
        f"INNER JOIN {_source_sql(a, t2)} AS b ON {_on_clause(a)}\n"
        f"WHERE (a.{col1} IS NULL) != (b.{col2} IS NULL)\n"
        f"   OR (a.{col1} IS NOT NULL AND b.{col2} IS NOT NULL AND a.{col1} != b.{col2});"
    )


ASSERTION_RENDERERS = {
    "row_predicate": render_row_predicate,
    "unique": render_unique,
    "cross_asset_row_count": render_cross_asset_row_count,
    "cross_asset_missing_rows": render_cross_asset_missing_rows,
    "cross_asset_column_equality": render_cross_asset_column_equality,
}


TEST_ASSERTION_TYPES = list(ASSERTION_RENDERERS.keys())


# ---------------------------------------------------------------------------
# REMOVED: apply_default_thresholds().
#
# This pipeline used to auto-fill {"metric": ..., "operator": "equals",
# "value": 0} for any row_predicate/unique/cross_asset_missing_rows spec
# that had evidence but no threshold -- on the reasoning that "zero
# violations" was the only sensible reading of an already-evidenced
# condition, not a number worth asking the LLM (or a human) to restate.
#
# Deliberately removed: a TestSpecification the LLM proposes without a
# threshold now simply stays without one. It still gets rendered and
# executed by run_test_specifications_and_store_results() below (that part
# of the design -- "no threshold means observe, don't fabricate a verdict"
# -- is unchanged), so the observed value is still visible to a human
# reviewer as a `no_threshold` result. What changed is narrower: code no
# longer invents the "equals 0" threshold on the model's behalf. If a
# verdict is wanted for one of these, either the LLM has to state the
# threshold itself, or a human reviewer adds it explicitly during review.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Core conversion: validated TestSpecifications -> executable SQL script.
# ---------------------------------------------------------------------------

def generate_sql_from_test_specifications(test_specs) -> str:
    """
    Takes a list of already-validated TestSpecification dicts and returns
    the full generated SQL script as a string, one query per specification,
    each tagged with the same "-- CHECK_ID: <id>" marker convention used by
    profiling.py so the two scripts can be parsed the same way.
    """
    blocks = []

    for spec in test_specs:
        spec_id = spec["id"]
        assertion = spec.get("assertion", {})
        a_type = assertion.get("type")
        renderer = ASSERTION_RENDERERS.get(a_type)

        threshold = spec.get("threshold")
        threshold_str = (
            f"{threshold['metric']} {threshold['operator']} {threshold['value']}"
            if threshold else "none (no explicit rule cited -- observation only)"
        )
        evidence_str = "; ".join(e.get("path", "") for e in spec.get("evidence", []))

        header = (
            f"-- CHECK_ID: {spec_id}\n"
            f"-- {'=' * 70}\n"
            f"-- ASSET: {spec['asset']}\n"
            f"-- TITLE: {spec.get('title', '')}\n"
            f"-- ASSERTION: {a_type}\n"
            f"-- STATUS: {spec.get('status', 'proposed')}\n"
            f"-- THRESHOLD: {threshold_str}\n"
            f"-- EVIDENCE: {evidence_str}\n"
            f"-- RATIONALE: {spec.get('rationale', '')}\n"
            f"-- {'=' * 70}\n"
        )
        # The header carries model-written text (id, title, rationale...): any line it breaks into stays a comment.
        header = "".join(line if line.startswith("--") else f"-- {line}" for line in header.splitlines(True))

        if renderer is None:
            blocks.append(header + f"-- TODO: no renderer defined for assertion type '{a_type}'")
            continue

        try:
            sql = renderer(spec)
        except (KeyError, ValueError) as e:
            blocks.append(header + f"-- TODO: could not render this specification: {e}")
            continue

        blocks.append(header + sql)

    return "\n\n".join(blocks).strip() + "\n"


# ---------------------------------------------------------------------------
# Human-readable description of what a TestSpecification actually checks.
#
# The LLM already supplies a `title` and `rationale` per spec (used in the
# SQL header comments above), but neither is stored in the results table or
# shown next to the verdict, so there's no way to look at the results
# and know *what a check was for* without cross-referencing the run
# manifest. This builds a second, deterministic description straight from
# the assertion's structural fields (column names, range bounds, accepted
# values, etc.) -- it can never be missing or hallucinated, since it's
# derived, not generated. Both the LLM's title and this description get
# stored and printed alongside every verdict.
# ---------------------------------------------------------------------------

def _describe_condition(node: dict) -> str:
    """Human-readable rendering of a row_predicate condition tree, mirroring
    _render_condition()'s grammar but producing prose instead of SQL."""
    op = node.get("op")

    def side_str(side):
        if "function" in side:  # now() / today(), or a column function like length(col)
            return f"{side['function']}({side.get('column', '')})"
        return side["column"] if "column" in side else repr(side.get("value"))

    if op in ("and", "or"):
        joiner = " and " if op == "and" else " or "
        return joiner.join(f"({_describe_condition(c)})" for c in node.get("clauses", []))
    if op == "implies":
        return f"if {_describe_condition(node['if'])}, then {_describe_condition(node['then'])}"
    if op == "is_null":
        return f"{side_str(node['left'])} is null"
    if op == "is_not_null":
        return f"{side_str(node['left'])} is not null"
    if op in ("in", "not_in"):
        values = ", ".join(str(v) for v in node.get("values", []))
        keyword = "is not one of" if op == "not_in" else "is one of"
        return f"{side_str(node['left'])} {keyword} [{values}]"
    symbols = {"eq": "==", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    if op in symbols:
        return f"{side_str(node['left'])} {symbols[op]} {side_str(node['right'])}"
    return f"<unrecognized condition: {node!r}>"


def _keys_str(a: dict) -> str:
    return ", ".join(left if left == right else f"{left}={right}" for left, right in join_pairs(a)) or "?"


def _filters_str(a: dict) -> str:
    parts = [f"{t} where {_describe_condition(c)}" for t, c in (a.get("filters") or {}).items() if c]
    return f" (only rows of {'; '.join(parts)})" if parts else ""


def _describe_assertion(spec: dict) -> str:
    a = spec.get("assertion", {})
    a_type = a.get("type")
    table = spec.get("asset", "?")

    if a_type == "row_predicate":
        return f"Confirms that for every row in {table}, {_describe_condition(a.get('condition', {}))}."
    if a_type == "unique":
        cols = ", ".join(a.get("columns", []))
        return f"Confirms {table}.[{cols}] contain no duplicate values."
    if a_type == "cross_asset_row_count":
        t1, t2 = a.get("tables", ["?", "?"])
        return f"Confirms {t1} and {t2} have matching row counts{_filters_str(a)}."
    if a_type == "cross_asset_missing_rows":
        t1, t2 = a.get("tables", ["?", "?"])
        return f"Confirms every row of {t1} has a match in {t2} on ({_keys_str(a)}){_filters_str(a)}."
    if a_type == "cross_asset_column_equality":
        t1, t2 = a.get("tables", ["?", "?"])
        col1, col2 = a.get("columns", ["?", "?"])
        return (
            f"Confirms {t1}.{col1} matches {t2}.{col2} for every row paired by "
            f"({_keys_str(a)}){_filters_str(a)}."
        )
    return f"Assertion type '{a_type}' on {table}."


# ---------------------------------------------------------------------------
# Compact "expected value" for a spec -- just the assertion's own varying
# parameters (a range, a values list, a condition), with none of the
# `_describe_assertion` sentence wrapping around it. This exists because
# `threshold_operator`/`threshold_value` in the results table, when present,
# is typically "equals 0" regardless of assertion type (see the "NOTE on
# what threshold means" prompt rule in build_test_specifier_prompt) -- that's the right
# semantics for "how many rows may violate this," but on its own it doesn't
# distinguish one check from another in the results table. This is what
# varies per test and is what a specialist scanning results actually wants
# to see at a glance, alongside (not instead of) the threshold columns.
# ---------------------------------------------------------------------------

def expected_value_str(spec: dict) -> str:
    a = spec.get("assertion", {})
    a_type = a.get("type")

    if a_type == "row_predicate":
        return _describe_condition(a.get("condition", {}))
    if a_type == "unique":
        return f"UNIQUE: {', '.join(a.get('columns', []))}"
    if a_type == "cross_asset_row_count":
        t1, t2 = a.get("tables", ["?", "?"])
        return f"row_count({t1}) == row_count({t2}){_filters_str(a)}"
    if a_type == "cross_asset_missing_rows":
        t1, t2 = a.get("tables", ["?", "?"])
        return f"every ({_keys_str(a)}) in {t1} exists in {t2}{_filters_str(a)}"
    if a_type == "cross_asset_column_equality":
        t1, t2 = a.get("tables", ["?", "?"])
        col1, col2 = a.get("columns", ["?", "?"])
        return f"{t1}.{col1} == {t2}.{col2}"
    return "?"


# Canonical dimension taxonomy this pipeline recognizes -- mirrors
# DQ_DIMENSIONS. Duplicated here (not imported) to keep this
# module usable standalone, the same way ASSERTION_RENDERERS etc. are
# self-contained; the two lists must be kept in sync by hand if the taxonomy
# ever changes.
_DQ_DIMENSIONS = ["completeness", "uniqueness", "validity", "consistency", "freshness", "accuracy"]


def _infer_dimension(spec: dict) -> str:
    """
    Best-effort dimension label for a TestSpecification or MetricThreshold,
    for the `dimension` column below.

    Neither object's schema has an explicit `dimension` field (the Test
    Specifier prompt never asks for one -- see build_test_specifier_prompt())
    -- the dimension only exists upstream, in the Dimension
    Selector's recommendation checklist, and that link isn't carried forward
    into the TestSpecification itself. So this is inference, not a stored
    fact, done two different ways depending on what's available:

      - MetricThreshold: its `check_id` is REQUIRED (by the same prompt) to
        be copied verbatim from the Observed Data Profile, whose check_ids
        are always shaped "<dimension>:<layer>.<table>:<column(s)>[:<check_type>]"
        (see parse_check_id()). So `check_id.split(":")[0]`
        is exact, not a guess, for this case.

      - TestSpecification: no such structured id exists. In practice the LLM
        consistently names single-table specs "<slug>_<dimension>" ending in
        one of the six canonical dimension names -- this checks for that
        suffix. cross_asset_* specs do not follow the
        suffix convention (e.g. "cross_landing_..._row_count") since they
        compare two tables rather than profile one column's dimension, but
        every cross_asset_* assertion type is itself a consistency check by
        construction (two layers must agree) -- so those fall back to
        "consistency" via assertion.type. Anything matching neither rule
        returns "" (unknown) rather than guessing further.
    """
    check_id = spec.get("check_id")
    if check_id:
        return check_id.split(":", 1)[0].strip().lower()

    # Explicit field (asked for since 2026-10-04) -- the inference below is
    # only a fallback for older runs that lack it.
    declared = (spec.get("dimension") or "").strip().lower()
    if declared in _DQ_DIMENSIONS:
        return declared

    # Dimension name anywhere in the slug ("..._completeness_order_id"),
    # not only as a suffix.
    tokens = set(re.split(r"[^a-z]+", (spec.get("id") or "").lower()))
    for dim in _DQ_DIMENSIONS:
        if dim in tokens:
            return dim

    a_type = spec.get("assertion", {}).get("type", "")
    if a_type.startswith("cross_asset"):
        return "consistency"

    return ""


def _evidence_str(spec: dict) -> str:
    """Same "path; path; ..." rendering already used for the SQL header's
    EVIDENCE comment in generate_sql_from_test_specifications(), reused here
    so the stored column and the generated SQL comment never disagree."""
    return "; ".join(e.get("path", "") for e in spec.get("evidence", []) or [])


def _compare(observed, operator, threshold_value):
    try:
        obs = float(observed)
        thr = float(threshold_value)
    except (TypeError, ValueError):
        return None  # non-numeric metric -- this simple runner can't compare it
    comparator = _COMPARATORS.get(operator)
    return comparator(obs, thr) if comparator else None


_VERDICT_LABEL = {
    "pass": "PASS",
    "fail": "FAIL",
    "no_threshold": "NO THRESHOLD (observed only)",
    "not_comparable": "NOT COMPARABLE",
    "error": "ERROR",
}


def print_results_report(report_rows) -> None:
    """
    Human-readable pass/fail report: for every executed TestSpecification or
    MetricThreshold, shows what it was checking (title + description), the
    verdict, and the observed value vs. the threshold that produced it.
    Complements the raw result rows -- this is what a
    specialist reading the console output actually wants to see. Public
    (not prefixed with `_`) so a caller running both TestSpecifications and
    MetricThresholds in the same run can combine their report_rows and print
    one unified summary instead of two separate ones -- see
    run_metric_thresholds_and_store_results() and its `print_report` param.
    """
    if not report_rows:
        return

    print(f"\n{'=' * 78}\nTEST RESULTS SUMMARY\n{'=' * 78}")

    counts = {}
    for row in report_rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1

    counts_str = ", ".join(
        f"{_VERDICT_LABEL.get(v, v)}: {c}" for v, c in counts.items()
    )
    print(counts_str + "\n")

    for row in report_rows:
        label = _VERDICT_LABEL.get(row["verdict"], row["verdict"].upper())
        title = row["title"] or "(no title)"
        print(f"[{label}] {row['check_id']} -- {title}")
        print(f"    asset:       {row['asset']}")
        print(f"    checks:      {row['description']}")
        if row.get("rationale"):
            print(f"    rationale:   {row['rationale']}")

        print(f"    expected:    {row['expected']}")

        if row["verdict"] == "error":
            print(f"    error:       {row['error']}")
        elif row["verdict"] == "no_threshold":
            print(f"    observed:    {row['observed']} (no threshold defined -- observation only)")
        else:
            thr = row["threshold"] or {}
            print(
                f"    observed:    {row['observed']}   "
                f"(allowed violations: {thr.get('metric', '?')} {thr.get('operator', '?')} {thr.get('value', '?')})"
            )
        print()


# ---------------------------------------------------------------------------
# MetricThreshold runner.
#
# Unlike a TestSpecification, a MetricThreshold never gets rendered into its
# own SQL and re-executed -- it targets a metric that THIS RUN's profiling
# pass (profiling.py + run_profiling()) already
# computed. "Executing" one is just a lookup against that in-memory result
# plus a comparison -- no second query against the source table, no new SQL
# renderer needed. This is what makes it possible to threshold metrics that
# don't fit any of the TestSpecification assertion types (cardinality
# ratios, outlier counts, staleness hours, functional-dependency violation
# counts, array-length stats...) without extending that fixed catalog.
# ---------------------------------------------------------------------------

def _describe_metric_threshold(spec: dict) -> str:
    return (
        f"Confirms profiling check '{spec.get('check_id')}' metric "
        f"'{spec.get('metric')}' stays {spec.get('operator')} {spec.get('value')}."
    )


def run_tests(wh, sql_text: str, specs_by_id: dict, context_level: str = "", progress=None) -> list:
    """
    Runs each "-- CHECK_ID: <spec id>" block produced by generate_sql_from_test_specifications (SELECT only) and
    returns one report row per specification: what it checks, the verdict (pass / fail / no_threshold /
    not_comparable / error), the observed value, the threshold and the SQL that ran. Nothing is stored here;
    the caller saves the rows with the run.
    """
    progress = progress or (lambda message: None)
    report_rows = []
    blocks = [b for b in re.split(r"-- CHECK_ID:", sql_text) if b.strip()]
    for i, block in enumerate(blocks, 1):
        progress(f"Running test {i} of {len(blocks)}")
        lines = block.strip().split("\n")
        check_id = lines[0].strip()
        sql_query = "\n".join(lines[1:]).strip()
        spec = specs_by_id.get(check_id)
        if spec is None:
            continue  # a TODO block (unsupported/unrenderable spec)
        if not sql_query.rstrip().endswith(";"):
            print(f"Warning: skipping unrenderable spec '{check_id}' (no executable SQL).")
            continue

        threshold = spec.get("threshold")
        error = observed = None
        shown = sql_query
        try:
            print(f"Executing test: {check_id} -> {spec['asset']}")
            shown = wh.render(sql_query)  # the SQL the database actually runs
            _, rows = wh.query(sql_query)
            observed = rows[0][0] if rows else None
        except Exception as e:
            error = str(e)
            print(f"ERROR: Failed to execute test '{check_id}'. Error: {e}")

        if error is not None:
            verdict = "error"
        elif threshold is None:
            verdict = "no_threshold"
        else:
            cmp_result = _compare(observed, threshold["operator"], threshold["value"])
            verdict = "not_comparable" if cmp_result is None else ("pass" if cmp_result else "fail")

        report_rows.append({
            "check_id": check_id,
            "asset": spec["asset"],
            "title": spec.get("title", ""),
            "description": _describe_assertion(spec),
            "rationale": spec.get("rationale", ""),
            "expected": expected_value_str(spec),
            "verdict": verdict,
            "observed": observed,
            "threshold": threshold,
            "error": error,
            "context_level": context_level,
            "dimension": _infer_dimension(spec),
            "evidence": _evidence_str(spec),
            "sql": shown,
        })
    return report_rows


def check_metric_thresholds(profiling_results: list, metric_thresholds: list, context_level: str = "") -> list:
    """
    Compares each validated MetricThreshold with the value this run's profiling already measured (the FIRST
    row of its check_id, as the profiling summary shows it) -- no extra query. Returns one report row each,
    in the same shape as run_tests() (assertion "metric_threshold", no SQL of its own).
    """
    profile_lookup = {}
    for row in profiling_results:
        profile_lookup.setdefault(row["check_id"], row["metrics"])

    report_rows = []
    for spec in metric_thresholds:
        check_id, metric = spec["check_id"], spec["metric"]
        operator, value = spec["operator"], spec["value"]
        metrics_for_check = profile_lookup.get(check_id)
        error = observed = None
        if metrics_for_check is None:
            error = f"check_id '{check_id}' not found in this run's profiling results"
        elif metric not in metrics_for_check:
            error = f"metric '{metric}' not found under check_id '{check_id}' in this run's profiling results"
        else:
            observed = metrics_for_check[metric]

        if error is not None:
            verdict = "error"
        else:
            cmp_result = _compare(observed, operator, value)
            verdict = "not_comparable" if cmp_result is None else ("pass" if cmp_result else "fail")

        report_rows.append({
            "check_id": spec["id"],
            "asset": spec["asset"],
            "title": spec.get("title", ""),
            "description": _describe_metric_threshold(spec),
            "rationale": spec.get("rationale", ""),
            "expected": f"{operator} {value}",
            "verdict": verdict,
            "observed": observed,
            "threshold": {"metric": metric, "operator": operator, "value": value},
            "error": error,
            "context_level": context_level,
            "dimension": _infer_dimension(spec),
            "evidence": _evidence_str(spec),
            "sql": "",
        })
    return report_rows
