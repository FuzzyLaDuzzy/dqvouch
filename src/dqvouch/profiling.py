"""Profiling: renders the accepted DQ dimensions into metric-only SQL (ClickHouse dialect), runs it, and condenses the
results. It never decides what is valid; it only makes the data visible to the Test Specifier and the validator."""
import json
import re

from dqvouch.render_sql import _render_condition, join_pairs, one_line


# Cap for the GROUP BY frequency tables (validity values, consistency
# combinations): only the top rows are used downstream (8 in the profiling
# summary, 1 for MetricThresholds), so more rows are only cost.
MAX_FREQUENCY_ROWS = 50


def sql_comment_block(lines):
    return "\n".join(f"-- {line}" for line in lines)


def inject_header(sql_text, header_lines):
    """
    Inserts `header_lines` (as "-- " prefixed comment lines) immediately
    after every "-- CHECK_ID: ..." line found in `sql_text`.

    This is the only place decorative/contextual comments (table name,
    dimension name, section banners) get added to generator output. It is
    deliberately done AFTER the CHECK_ID line -- never before -- so the
    text always lands inside the block it describes rather than leaking
    into the tail of the previous block when the consumer splits on
    "-- CHECK_ID:".
    """
    if not header_lines:
        return sql_text
    header_comment = "\n".join(f"-- {line}" for line in header_lines)

    def _insert(match):
        return f"{match.group(0)}\n{header_comment}"

    return re.sub(r"-- CHECK_ID: [^\n]*", _insert, sql_text)


# ---------------------------------------------------------------------------
# Column type helpers -- used to gate type-specific metrics (string-only,
# etc.) so we never emit SQL that's guaranteed to fail against the wrong
# type. `column_types` is always a plain {column_name: declared_type} dict;
# an empty dict (no AssetContext available) just means every type-gated
# extra is skipped, not that anything breaks.
# ---------------------------------------------------------------------------

def _column_types_for_table(asset_contexts, table):
    if not asset_contexts:
        return {}
    ctx = asset_contexts.get(table, {})
    return {c.get("name"): c.get("type", "") for c in ctx.get("columns", []) if c.get("name")}


def metric_alias(col: str) -> str:
    """Column name -> safe alias prefix. Nested columns ("sources.uri") can't
    appear bare in an alias (`AS sources.uri_nulls` is Syntax error 62), so
    every non-identifier character becomes '_'. Consumers that look a
    metric up by column name (validator.py) use this too."""
    return re.sub(r"\W", "_", col)


def _is_string_type(col_type: str) -> bool:
    return any(k in col_type for k in ("String", "Enum", "FixedString"))


def _is_numeric_type(col_type: str) -> bool:
    return any(k in col_type for k in ("Int", "Float", "Decimal"))


def _is_array_type(col_type: str) -> bool:
    return "Array(" in col_type


# ---------------------------------------------------------------------------
# Per-dimension SQL generators -- metrics only, no thresholds, no guessed
# valid-value domains. `rationale` is carried through purely as a comment
# for human context; it is never parsed for values. `column_types` is an
# optional {name: declared_type} dict (see helpers above) used to gate
# type-specific extras.
# ---------------------------------------------------------------------------

def gen_completeness(table, columns, rationale, column_types=None):
    column_types = column_types or {}
    check_id = f"completeness:{table}:{','.join(columns)}"

    fields = []
    for col in columns:
        fields.append(f"    countIf({col} IS NULL) AS {metric_alias(col)}_nulls")
        fields.append(
            f"    round(countIf({col} IS NULL) / count() * 100, 2) AS {metric_alias(col)}_null_rate_pct"
        )
    body = ",\n".join(fields)
    nulls_query = (
        f"-- CHECK_ID: {check_id}\n"
        f"-- Completeness metrics for: {', '.join(columns)}\n"
        f"-- Rationale: {rationale}\n"
        f"SELECT\n"
        f"    count() AS total_rows,\n{body}\n"
        f"FROM {table};"
    )

    db, tbl = table.split(".", 1)
    cols_list = ", ".join(f"'{c}'" for c in columns)
    metadata_query = (
        f"-- CHECK_ID: {check_id}:metadata\n"
        f"-- Completeness: declared type & nullable flag (from system.columns metadata)\n"
        f"SELECT\n"
        f"    name AS declared_column_name,\n"
        f"    type AS declared_type,\n"
        f"    type LIKE 'Nullable%' AS is_nullable\n"
        f"FROM system.columns\n"
        f"WHERE database = '{db}' AND table = '{tbl}'\n"
        f"  AND name IN ({cols_list});"
    )

    blocks = [nulls_query, metadata_query]

    # Blank/whitespace-only string counts, String-typed columns only: a
    # non-null column can still contain an empty string, which the null-count
    # query above cannot see.
    # _is_string_type also matches Enum8/Enum16 (correct -- those ARE
    # categorical/string-like for gating purposes), but ClickHouse's trim()
    # rejects an Enum argument directly (ILLEGAL_TYPE_OF_ARGUMENT) -- wrap in
    # toString() so this works for both plain String columns (a harmless
    # no-op cast) and Enum columns (the cast ClickHouse actually requires).
    string_cols = [c for c in columns if _is_string_type(column_types.get(c, ""))]
    if string_cols:
        blank_fields = []
        for col in string_cols:
            blank_fields.append(f"    countIf({col} IS NOT NULL AND trim(toString({col})) = '') AS {metric_alias(col)}_blank_count")
            blank_fields.append(
                f"    round(countIf({col} IS NOT NULL AND trim(toString({col})) = '') / count() * 100, 2) AS {metric_alias(col)}_blank_rate_pct"
            )
        blank_body = ",\n".join(blank_fields)
        blank_query = (
            f"-- CHECK_ID: {check_id}:blank_strings\n"
            f"-- Completeness: empty/whitespace-only string counts for: {', '.join(string_cols)}\n"
            f"-- (a NULL check alone misses this -- 'a non-null column can still contain an empty string')\n"
            f"-- Rationale: {rationale}\n"
            f"SELECT\n"
            f"    count() AS total_rows,\n{blank_body}\n"
            f"FROM {table};"
        )
        blocks.append(blank_query)

    # Empty-array counts, Array-typed columns only. Same idea as the
    # blank-string check above: an empty array is a completeness gap a NULL
    # check alone can't see (e.g. an 'attributes' tag list that came back
    # empty because upstream extraction silently failed).
    array_cols = [c for c in columns if _is_array_type(column_types.get(c, ""))]
    if array_cols:
        empty_fields = []
        for col in array_cols:
            empty_fields.append(f"    countIf(empty({col})) AS {metric_alias(col)}_empty_array_count")
            empty_fields.append(
                f"    round(countIf(empty({col})) / count() * 100, 2) AS {metric_alias(col)}_empty_array_rate_pct"
            )
        empty_body = ",\n".join(empty_fields)
        empty_query = (
            f"-- CHECK_ID: {check_id}:empty_arrays\n"
            f"-- Completeness: empty-array counts for: {', '.join(array_cols)}\n"
            f"-- (an empty array is a completeness gap just like NULL or a blank string)\n"
            f"-- Rationale: {rationale}\n"
            f"SELECT\n"
            f"    count() AS total_rows,\n{empty_body}\n"
            f"FROM {table};"
        )
        blocks.append(empty_query)

    return "\n\n".join(blocks)


def gen_uniqueness(table, columns, rationale, column_types=None):
    check_id = f"uniqueness:{table}:{','.join(columns)}"

    fields = []
    for col in columns:
        fields.append(f"    uniqExact({col}) AS {metric_alias(col)}_distinct_count")
        fields.append(f"    count() - uniqExact({col}) AS {metric_alias(col)}_duplicate_count")
        # Null count per candidate-key column: nulls make dedup
        # semantics ambiguous (is NULL a "duplicate" of another NULL?), so a
        # specialist needs to see this before trusting the counts above.
        fields.append(f"    countIf({col} IS NULL) AS {metric_alias(col)}_null_count")
    body = ",\n".join(fields)
    per_column_query = (
        f"-- CHECK_ID: {check_id}\n"
        f"-- Uniqueness metrics for: {', '.join(columns)}\n"
        f"-- Rationale: {rationale}\n"
        f"SELECT\n"
        f"    count() AS total_rows,\n{body}\n"
        f"FROM {table};"
    )
    blocks = [per_column_query]

    # Composite-key (grain) duplicate count when multiple columns are
    # proposed together: the table's real uniqueness
    # grain is frequently a COMBINATION of columns, not any single one of
    # them checked in isolation (which is all the per-column query above
    # can tell you).
    if len(columns) > 1:
        cols_str = ", ".join(columns)
        composite_query = (
            f"-- CHECK_ID: {check_id}:composite_key\n"
            f"-- Uniqueness: composite-key duplicate count across ({cols_str}) treated as one grain\n"
            f"-- Rationale: {rationale}\n"
            f"SELECT\n"
            f"    count() AS total_rows,\n"
            f"    uniqExact(tuple({cols_str})) AS composite_distinct_count,\n"
            f"    count() - uniqExact(tuple({cols_str})) AS composite_duplicate_count\n"
            f"FROM {table};"
        )
        blocks.append(composite_query)

    return "\n\n".join(blocks)


def gen_freshness(table, columns, rationale, column_types=None):
    queries = []
    for col in columns:
        check_id = f"freshness:{table}:{col}"
        queries.append(
            f"-- CHECK_ID: {check_id}\n"
            f"-- Freshness metrics for '{col}'\n"
            f"-- Rationale: {rationale}\n"
            f"-- NOTE: no staleness threshold is applied here -- this only reports\n"
            f"-- the observed recency; decide what counts as 'stale' downstream.\n"
            f"SELECT\n"
            f"    max({col}) AS most_recent_{col},\n"
            f"    min({col}) AS oldest_{col},\n"
            f"    dateDiff('hour', max({col}), now()) AS hours_since_last_update\n"
            f"FROM {table};"
        )

        # Future-dated values and nulls. min/max alone can hide a
        # handful of clock-skew or bad-backfill rows sitting past `now()`
        # among otherwise-normal dates; this makes them visible directly,
        # and a null count flags rows freshness can't be assessed for at all.
        queries.append(
            f"-- CHECK_ID: {check_id}:future_values\n"
            f"-- Freshness: rows with '{col}' dated in the future (possible clock skew or bad backfill)\n"
            f"-- Rationale: {rationale}\n"
            f"SELECT\n"
            f"    countIf(toDateTime({col}) > now()) AS {metric_alias(col)}_future_count,\n"
            f"    countIf({col} IS NULL) AS {metric_alias(col)}_null_count\n"
            f"FROM {table};"
        )
    return "\n\n".join(queries)


def gen_validity(table, columns, rationale, column_types=None):
    """Reports the ACTUAL observed value distribution -- it does not assume
    or guess which values count as 'valid'. That decision is left to a human
    or a downstream rules step reading this output."""
    column_types = column_types or {}
    queries = []
    for col in columns:
        check_id = f"validity:{table}:{col}"
        queries.append(
            f"-- CHECK_ID: {check_id}\n"
            f"-- Validity metrics for '{col}': observed value frequency distribution\n"
            f"-- Rationale: {rationale}\n"
            f"-- Inspect this output to decide which values are valid.\n"
            f"SELECT\n"
            f"    {col} AS value,\n"
            f"    count() AS occurrences,\n"
            f"    round(count() * 100.0 / sum(count()) OVER (), 2) AS pct_of_total\n"
            f"FROM {table}\n"
            f"GROUP BY {col}\n"
            f"ORDER BY occurrences DESC\n"
            f"LIMIT {MAX_FREQUENCY_ROWS};"  # pct_of_total's window is computed before LIMIT
        )

        # Cardinality summary. A quick distinct-value-ratio tells a
        # specialist whether this column looks enum-like (low cardinality
        # -> candidate for an accepted_values test) or closer to free text /
        # an identifier (high cardinality -> a format/range test fits
        # better), without having to eyeball the full frequency table above.
        queries.append(
            f"-- CHECK_ID: {check_id}:cardinality\n"
            f"-- Validity: cardinality summary for '{col}' (low distinct_ratio_pct suggests an\n"
            f"-- enumerated domain; high suggests free text or an identifier)\n"
            f"-- Rationale: {rationale}\n"
            f"SELECT\n"
            f"    count() AS total_rows,\n"
            f"    uniqExact({col}) AS distinct_values,\n"
            f"    round(uniqExact({col}) / count() * 100, 2) AS distinct_ratio_pct\n"
            f"FROM {table};"
        )

        # String length distribution, String-typed columns only.
        # Surfaces format outliers (truncated/malformed values) that can get
        # buried among many distinct values in the frequency table above.
        # Same note as gen_completeness's blank-string check: _is_string_type
        # also matches Enum8/Enum16 on purpose, but ClickHouse's length()
        # rejects an Enum argument directly (ILLEGAL_TYPE_OF_ARGUMENT) --
        # toString() makes this work for both plain String columns and Enum
        # columns alike.
        if _is_string_type(column_types.get(col, "")):
            queries.append(
                f"-- CHECK_ID: {check_id}:length\n"
                f"-- Validity: string length distribution for '{col}'\n"
                f"-- Rationale: {rationale}\n"
                f"SELECT\n"
                f"    min(length(toString({col}))) AS min_length,\n"
                f"    max(length(toString({col}))) AS max_length,\n"
                f"    round(avg(length(toString({col}))), 2) AS avg_length\n"
                f"FROM {table};"
            )

        # Array-length distribution + observed element frequency,
        # Array-typed columns only (e.g. a tag/attribute list). The default
        # per-value frequency query above groups by the whole array, which
        # is nearly useless for arrays (every row's combination is close to
        # unique) -- this instead looks at length distribution and the
        # actual vocabulary of individual elements, capped at the top 50 so
        # a large tag vocabulary doesn't balloon the output.
        if _is_array_type(column_types.get(col, "")):
            queries.append(
                f"-- CHECK_ID: {check_id}:array_length\n"
                f"-- Validity: array-length distribution for '{col}'\n"
                f"-- Rationale: {rationale}\n"
                f"SELECT\n"
                f"    min(length({col})) AS min_array_length,\n"
                f"    max(length({col})) AS max_array_length,\n"
                f"    round(avg(length({col})), 2) AS avg_array_length\n"
                f"FROM {table};"
            )
            queries.append(
                f"-- CHECK_ID: {check_id}:array_elements\n"
                f"-- Validity: observed element frequency for '{col}' (top 50) -- inspect to\n"
                f"-- build an accepted_values-style vocabulary for this array's elements.\n"
                f"-- Rationale: {rationale}\n"
                f"SELECT\n"
                f"    arrayJoin({col}) AS element,\n"
                f"    count() AS occurrences\n"
                f"FROM {table}\n"
                f"GROUP BY element\n"
                f"ORDER BY occurrences DESC\n"
                f"LIMIT 50;"
            )
    return "\n\n".join(queries)


def gen_accuracy(table, columns, rationale, column_types=None):
    """Reports distributional statistics only -- no bounds are assumed or
    guessed. Use the output to decide what an accurate range looks like."""
    column_types = column_types or {}
    queries = []
    for col in columns:
        check_id = f"accuracy:{table}:{col}"
        col_type = column_types.get(col, "")

        # Every metric below (avg, stddevPop, median, quantile, the
        # sign comparisons, the outlier detection) is numeric-only.
        # ClickHouse rejects all of them against a String/Enum/Array column
        # (observed: "Illegal type String of argument for aggregate function
        # avg"; "no supertype for String, UInt8"). When the column's KNOWN
        # type is non-numeric, skip straight to a redirect note instead of
        # emitting SQL that's guaranteed to error -- if the real concern was
        # format/pattern correctness rather than a numeric range, Validity's
        # frequency table, cardinality, and length-distribution checks are
        # the right tool for that, not Accuracy. When the type is unknown
        # (no asset_contexts available, e.g. standalone CLI use), fall back
        # to the original behavior rather than guessing either way.
        if col_type and (_is_string_type(col_type) or _is_array_type(col_type)):
            queries.append(
                f"-- CHECK_ID: {check_id}:not_applicable\n"
                f"-- Accuracy: '{col}' is declared as {col_type} -- Accuracy's distributional\n"
                f"-- metrics (avg, stddev, outliers) require a numeric type and do not apply.\n"
                f"-- Rationale: {rationale}\n"
                f"-- TODO: if format/pattern correctness was the actual concern, propose this\n"
                f"-- column under Validity instead -- its frequency table, cardinality, and\n"
                f"-- length-distribution checks already cover exactly that for non-numeric columns."
            )
            continue

        queries.append(
            f"-- CHECK_ID: {check_id}\n"
            f"-- Accuracy metrics for '{col}': distributional statistics\n"
            f"-- Rationale: {rationale}\n"
            f"SELECT\n"
            f"    count() AS total_rows,\n"
            f"    min({col}) AS min_value,\n"
            f"    max({col}) AS max_value,\n"
            f"    round(avg({col}), 4) AS avg_value,\n"
            f"    round(stddevPop({col}), 4) AS stddev_value,\n"
            f"    median({col}) AS median_value,\n"
            f"    quantile(0.01)({col}) AS p01_value,\n"
            f"    quantile(0.99)({col}) AS p99_value\n"
            f"FROM {table};"
        )

        # Sign counts. An unexpected negative or zero value on a
        # column that's semantically a score/amount/count is one of the
        # cheapest, most concrete Accuracy signals available, and min/max
        # alone can hide it if it's not the extreme value.
        queries.append(
            f"-- CHECK_ID: {check_id}:sign\n"
            f"-- Accuracy: negative/zero value counts for '{col}'\n"
            f"-- Rationale: {rationale}\n"
            f"SELECT\n"
            f"    countIf({col} < 0) AS {metric_alias(col)}_negative_count,\n"
            f"    countIf({col} = 0) AS {metric_alias(col)}_zero_count\n"
            f"FROM {table};"
        )

        # Outlier count beyond 3 standard deviations from the mean.
        # A single global avg/stddev (from the first query) doesn't tell you
        # HOW MANY rows are actually extreme; this does, using window
        # functions so it stays a single pass over the data.
        #
        # NOTE: the window functions must be computed in a subquery -- ClickHouse
        # rejects a window function nested directly inside an aggregate function
        # in the same SELECT (code 184, ILLEGAL_AGGREGATION: "Window function ...
        # is found inside an aggregate function"). countIf() over the boolean
        # flag from the inner query is fine; it's the outer aggregate wrapping
        # the window call directly that's illegal.
        queries.append(
            f"-- CHECK_ID: {check_id}:outliers\n"
            f"-- Accuracy: rows more than 3 standard deviations from the mean for '{col}'\n"
            f"-- Rationale: {rationale}\n"
            f"SELECT countIf(is_outlier) AS {metric_alias(col)}_outliers_3stddev\n"
            f"FROM (\n"
            f"    SELECT abs({col} - avg({col}) OVER ()) > 3 * stddevPop({col}) OVER () AS is_outlier\n"
            f"    FROM {table}\n"
            f"    WHERE {col} IS NOT NULL\n"
            f") AS outlier_calc;"
        )
    return "\n\n".join(queries)


def gen_consistency(table, columns, rationale, column_types=None):
    """Reports the observed co-occurrence of the flagged columns -- it does
    not assume which combinations are 'consistent'. Inspect the frequency
    table to decide."""
    column_types = column_types or {}
    cols_str = ", ".join(columns)
    check_id = f"consistency:{table}:{cols_str}"
    co_occurrence_query = (
        f"-- CHECK_ID: {check_id}\n"
        f"-- Consistency metrics across columns: {cols_str}\n"
        f"-- Rationale: {rationale}\n"
        f"-- Observed combination frequency -- inspect to spot inconsistent pairings.\n"
        f"SELECT\n"
        f"    {cols_str},\n"
        f"    count() AS occurrences\n"
        f"FROM {table}\n"
        f"GROUP BY {cols_str}\n"
        f"ORDER BY occurrences DESC\n"
        # Capped: high-cardinality combinations (e.g. 8 columns incl. hashes and
        # timestamps) returned ~700k rows -- hours of
        # profiling. Only the top rows are ever used downstream
        # (build_profiling_summary keeps 8, MetricThresholds read the first).
        f"LIMIT {MAX_FREQUENCY_ROWS};"
    )
    blocks = [co_occurrence_query]

    # Per-column null counts alongside the co-occurrence table. The
    # co-occurrence table groups NULL in with every other observed value,
    # which can bury a real "these columns disagree about presence" signal
    # among many distinct combinations.
    null_fields = ",\n".join(f"    countIf({c} IS NULL) AS {metric_alias(c)}_nulls" for c in columns)
    null_query = (
        f"-- CHECK_ID: {check_id}:null_alignment\n"
        f"-- Consistency: null counts per column, to spot columns disagreeing about presence\n"
        f"-- Rationale: {rationale}\n"
        f"SELECT\n"
        f"    count() AS total_rows,\n{null_fields}\n"
        f"FROM {table};"
    )
    blocks.append(null_query)

    # Explicit null-mismatch count for the common 2-column case
    # (kept to exactly 2 columns to keep the boolean expression simple and
    # unambiguous; for >2 columns the null-count query above is still
    # informative on its own).
    if len(columns) == 2:
        c1, c2 = columns
        mismatch_query = (
            f"-- CHECK_ID: {check_id}:null_mismatch\n"
            f"-- Consistency: rows where exactly one of ({c1}, {c2}) is null (nullness disagreement)\n"
            f"-- Rationale: {rationale}\n"
            f"SELECT countIf(({c1} IS NULL) != ({c2} IS NULL)) AS null_mismatch_rows\n"
            f"FROM {table};"
        )
        blocks.append(mismatch_query)

        # Functional-dependency violation counts, both directions. A
        # real business rule is often "this ID always maps to the same
        # name/label" (e.g. product_id -> product_name) -- neither the
        # co-occurrence table nor the null-mismatch count above actually
        # answers that; this does, directly. Checked in both directions
        # since which column determines the other isn't implied by the
        # order the columns happen to be listed in. A genuine 1:1 mapping
        # shows 0 on one (or both) sides -- strong, concrete evidence for a
        # row_rule test asserting that pairing should never diverge.
        fd_query = (
            f"-- CHECK_ID: {check_id}:functional_dependency\n"
            f"-- Consistency: functional-dependency check between {c1} and {c2} -- how many\n"
            f"-- distinct {c1} values map to more than one {c2} value, and vice versa.\n"
            f"-- Rationale: {rationale}\n"
            f"SELECT\n"
            f"    (SELECT count() FROM (\n"
            f"        SELECT {c1} FROM {table} WHERE {c1} IS NOT NULL\n"
            f"        GROUP BY {c1} HAVING uniqExact({c2}) > 1\n"
            f"    )) AS {metric_alias(c1)}_values_with_multiple_{metric_alias(c2)},\n"
            f"    (SELECT count() FROM (\n"
            f"        SELECT {c2} FROM {table} WHERE {c2} IS NOT NULL\n"
            f"        GROUP BY {c2} HAVING uniqExact({c1}) > 1\n"
            f"    )) AS {metric_alias(c2)}_values_with_multiple_{metric_alias(c1)};"
        )
        blocks.append(fd_query)

        # Grouped numeric-by-category stats, when one column is
        # numeric and the other categorical/string. This is what actually
        # shows a specialist whether a categorical value cleanly separates a
        # numeric column's range (e.g. a classification label separating a
        # score column into two non-overlapping bands) -- real, observed
        # evidence for a threshold-based row_rule test, which none of the
        # other Consistency metrics above surface on their own.
        numeric_col, category_col = None, None
        if _is_numeric_type(column_types.get(c1, "")) and _is_string_type(column_types.get(c2, "")):
            numeric_col, category_col = c1, c2
        elif _is_numeric_type(column_types.get(c2, "")) and _is_string_type(column_types.get(c1, "")):
            numeric_col, category_col = c2, c1

        if numeric_col and category_col:
            grouped_query = (
                f"-- CHECK_ID: {check_id}:grouped_numeric_stats\n"
                f"-- Consistency: distribution of numeric '{numeric_col}' grouped by categorical\n"
                f"-- '{category_col}' -- inspect for a clean separation between categories.\n"
                f"-- Rationale: {rationale}\n"
                f"SELECT\n"
                f"    {category_col} AS category,\n"
                f"    count() AS row_count,\n"
                f"    min({numeric_col}) AS min_value,\n"
                f"    max({numeric_col}) AS max_value,\n"
                f"    round(avg({numeric_col}), 4) AS avg_value\n"
                f"FROM {table}\n"
                f"GROUP BY {category_col}\n"
                f"ORDER BY row_count DESC\n"
                f"LIMIT {MAX_FREQUENCY_ROWS};"
            )
            blocks.append(grouped_query)

    return "\n\n".join(blocks)


DIMENSION_GENERATORS = {
    "Completeness": gen_completeness,
    "Uniqueness": gen_uniqueness,
    "Freshness": gen_freshness,
    "Validity": gen_validity,
    "Accuracy": gen_accuracy,
    "Consistency": gen_consistency,
}


# ---------------------------------------------------------------------------
# Cross-table checks -- structural (row counts, join-based matching), not
# threshold-based, so these stay as direct comparisons.
# ---------------------------------------------------------------------------

def gen_cross_table_check(check):
    tables = check["tables_involved"]
    columns = check["columns_involved"]
    rationale = one_line(check.get("rationale", ""))
    # choose_cross_table_join_keys() sets the most-distinct shared column; the first
    # listed column is only the fallback (a single-value column turns the join into N x M rows).
    join_key = check.get("join_key") or columns[0]
    check_id = f"consistency:cross-table:{'_vs_'.join(tables)}:{','.join(columns)}"

    if len(tables) != 2:
        # No CHECK_ID here on purpose -- this isn't runnable SQL, it's a
        # manual-follow-up note. Keeping it markerless prevents the consumer
        # from mistaking it for a (malformed) check block. It will simply be
        # skipped over during parsing since it lives inside a larger section
        # that always starts with a proper "-- CHECK_ID:" marker.
        return sql_comment_block([
            "TODO: cross_table_check involves != 2 tables, please write manually.",
            f"Tables: {tables}", f"Columns: {columns}", f"Rationale: {rationale}",
        ])

    t1, t2 = tables
    header_lines = [
        f"Cross-table Consistency metrics: {t1}  vs  {t2}",
        f"Columns involved: {', '.join(columns)}",
        f"Rationale: {rationale}",
        (f"Join key: '{join_key}' (shared column with the most distinct values)." if check.get("join_key")
         else f"NOTE: join key defaulted to '{join_key}' (first listed column) -- verify this is correct."),
        "Joins are ANTI / one-row-per-key so a non-unique key can't fan out into N x M rows "
        "(a single-value key column would multiply the rows of both tables).",
    ]

    row_count_query = (
        f"SELECT\n"
        f"    (SELECT count() FROM {t1}) AS {t1.split('.')[-1]}_count,\n"
        f"    (SELECT count() FROM {t2}) AS {t2.split('.')[-1]}_count,\n"
        f"    (SELECT count() FROM {t1}) - (SELECT count() FROM {t2}) AS row_count_diff;"
    )
    row_count_block = f"-- CHECK_ID: {check_id}:row_counts\n{row_count_query}"
    row_count_block = inject_header(row_count_block, header_lines)

    missing_query = (
        f"-- Rows present in {t1} but missing from {t2} (by {join_key})\n"
        f"SELECT count() AS missing_count\n"
        f"FROM {t1} AS a\n"
        f"LEFT ANTI JOIN {t2} AS b ON a.{join_key} = b.{join_key};"
    )
    missing_query_block = f"-- CHECK_ID: {check_id}:missing_rows\n{missing_query}"
    missing_query_block = inject_header(missing_query_block, header_lines)

    mismatch_fields = [c for c in check.get("shared_columns", columns) if c != join_key]
    mismatch_query_block = ""
    if mismatch_fields and check.get("join_key_identifies_rows", True):
        conds = " OR ".join(f"a.{c} != b.{c}" for c in mismatch_fields)
        mismatch_query = (
            f"-- Rows where {', '.join(mismatch_fields)} differ between the two layers\n"
            f"SELECT count() AS mismatch_count\n"
            f"FROM {t1} AS a\n"
            # One t2 row per key (LIMIT 1 BY) keeps this <= rows(t1). Not ANY JOIN:
            # it crashed ClickHouse 25.1.2 on large tables.
            f"INNER JOIN (SELECT {', '.join([join_key] + mismatch_fields)} FROM {t2} "
            f"LIMIT 1 BY {join_key}) AS b ON a.{join_key} = b.{join_key}\n"
            f"WHERE {conds};"
        )
        mismatch_query_block = f"-- CHECK_ID: {check_id}:mismatched_values\n{mismatch_query}"
        mismatch_query_block = inject_header(mismatch_query_block, header_lines)

    blocks = [row_count_block, missing_query_block]
    if mismatch_query_block:
        blocks.append(mismatch_query_block)
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def generate_sql_from_dq_json(data: dict, asset_contexts: dict = None) -> str:
    """Takes the parsed DQ dimension map JSON (a dict, already loaded from
    the LLM's JSON code block) and returns the full generated SQL as a
    string. Raises no exceptions for unknown dimensions -- those become a
    TODO comment so the run never silently drops a check.

    `asset_contexts`, if provided (the pipeline always passes it),
    lets the per-dimension generators look up each column's declared type
    and gate type-specific metrics (e.g. blank-string checks only for String
    columns). Without it, those extras are simply skipped -- everything
    else still runs.

    Layout note: every section of contextual/decorative comment text
    (table name, dimension name, section banners) is injected AFTER the
    "-- CHECK_ID:" marker of the block it describes via inject_header(),
    never appended ahead of one. This keeps every block self-contained and
    semicolon-terminated for the downstream consumer, which splits strictly
    on "-- CHECK_ID:".
    """
    out_sections = []

    for table_entry in data.get("tables", []):
        table = table_entry["table"]
        column_types = _column_types_for_table(asset_contexts, table)

        for dim in table_entry.get("dimensions", []):
            dimension = dim["dimension"]
            columns = dim.get("columns", [])
            rationale = one_line(dim.get("rationale", ""))

            generator = DIMENSION_GENERATORS.get(dimension)
            header_lines = [
                "=" * 70,
                f"TABLE: {table}",
                f"DIMENSION: {dimension}",
                "STATUS: proposed",
                "=" * 70,
            ]

            if generator is None:
                # Give it a proper CHECK_ID marker (with no trailing ';')
                # so the consumer isolates it as its own block and cleanly
                # skips it as "malformed" (i.e. not executable SQL) instead
                # of it leaking into a neighboring block's boundary.
                todo_block = (
                    f"-- CHECK_ID: {dimension.lower()}:{table}:unsupported_dimension\n"
                    + sql_comment_block([
                        f"TODO: no generator defined for dimension '{dimension}'",
                    ])
                )
                todo_block = inject_header(todo_block, header_lines)
                out_sections.append("\n" + todo_block)
                continue

            sql_text = generator(table, columns, rationale, column_types)
            sql_text = inject_header(sql_text, header_lines)
            out_sections.append("\n" + sql_text)

    if data.get("cross_table_checks"):
        for check in data["cross_table_checks"]:
            cross_header_lines = ["=" * 70, "CROSS-TABLE CHECK", "STATUS: proposed", "=" * 70]
            block = gen_cross_table_check(check)
            block = inject_header(block, cross_header_lines)
            out_sections.append("\n" + block)

    return "\n".join(out_sections).strip() + "\n"


# The six DQ dimensions this pipeline recognizes. "freshness" plays the role of
# Timeliness; "Schema conformity" is not a separate dimension; "accuracy" is
# distributional statistics only, not reconciliation against a source of truth.
DQ_DIMENSIONS = ["completeness", "uniqueness", "validity", "consistency", "freshness", "accuracy"]


def parse_check_id(check_id: str) -> dict:
    """
    Splits a check_id into its component fields instead of storing it as one
    opaque string.

    This matches the actual format produced by generate_sql_from_dq_json,
    which uses a single ':' as the field separator and has two shapes:

    Normal (single-table) check:
        <dimension>:<layer>.<table>:<column(s)>[:<check_type>]
        e.g. "completeness:landing.orders_raw:order_id,status"
        e.g. "completeness:landing.orders_raw:order_id,status:metadata"

    Cross-table check:
        <dimension>:cross-table:<table1>_vs_<table2>:<column(s)>:<check_type>
        e.g. "consistency:cross-table:landing.orders_raw_vs_staging.orders_trans:order_id:row_counts"

    column(s) may be a comma-separated list (e.g. "order_id,status") -- that
    full string is kept as-is in column_name rather than split further.
    """
    parts = check_id.split(":")
    dimension = parts[0].strip().lower() if parts else ""

    if len(parts) > 1 and parts[1] == "cross-table":
        layer = "cross-table"
        table_name = parts[2] if len(parts) > 2 else ""
        column_name = parts[3] if len(parts) > 3 else ""
        check_type = ":".join(parts[4:]) if len(parts) > 4 else ""
    else:
        layer_table = parts[1] if len(parts) > 1 else ""
        if "." in layer_table:
            layer, table_name = layer_table.split(".", 1)
        else:
            layer, table_name = "", layer_table
        column_name = parts[2] if len(parts) > 2 else ""
        check_type = ":".join(parts[3:]) if len(parts) > 3 else ""

    return {
        "dimension": dimension,
        "layer": layer,
        "table_name": table_name,
        "column_name": column_name,
        "check_type": check_type,
    }


def choose_cross_table_join_keys(dim_accepted: dict, asset_contexts: dict, wh) -> None:
    """Sets check["join_key"] on each 2-table cross_table_check to the column
    present in both tables with the most distinct values in the first table.
    Data-driven, no names assumed: a low-cardinality key (one with a single
    value) turns the profiling join into N x M rows."""
    checks = [c for c in dim_accepted.get("cross_table_checks", []) or []
              if len(c.get("tables_involved", [])) == 2]
    if not checks:
        return
    for check in checks:
        t1, t2 = check["tables_involved"]
        cols_of = lambda t: {c["name"] for c in asset_contexts.get(t, {}).get("columns", [])}
        shared = [c for c in check.get("columns_involved", []) if c in cols_of(t1) and c in cols_of(t2)]
        if not shared:
            continue
        check["shared_columns"] = shared  # mismatched_values can only compare these
        try:
            *row, n_rows = wh.query("SELECT " + ", ".join(f"uniqExact(`{c}`)" for c in shared)
                                        + f", count() FROM {t1}")[1][0]
        except Exception as e:
            print(f"  join key for {t1} vs {t2}: could not measure ({e}); using first listed column")
            continue
        check["join_key"], distinct = max(zip(shared, row), key=lambda cv: cv[1])  # ties keep listed order
        # Pairing rows by a non-identifying key makes mismatched_values noise (1.2M "mismatches").
        check["join_key_identifies_rows"] = distinct >= 0.99 * n_rows
        print(f"  join key for {t1} vs {t2}: {check['join_key']} ({distinct:,} distinct / {n_rows:,} rows)"
              + ("" if check["join_key_identifies_rows"] else " -- not unique, mismatched_values skipped"))


def add_join_key_stats(profiling_summary, test_specs, asset_contexts, wh) -> None:
    """Adds `join_key_stats:<table>:<col>` entries (row count, NULL count) to the profiling
    summary for every column used in a proposed cross-asset join_key, so the validator can
    reject keys that are NULL in many rows (a NULL key never matches). The Dimension Selector's
    profiling rarely covers these columns, hence this small extra measurement."""
    if profiling_summary is None:
        return
    wanted, uniques, in_lists, not_nulls = set(), set(), set(), set()
    for spec in test_specs or []:
        a = spec.get("assertion") or {}
        names = {c["name"] for c in asset_contexts.get(spec.get("asset"), {}).get("columns", [])}
        cols = a.get("columns") if a.get("type") == "unique" else None
        if isinstance(cols, list) and cols and all(isinstance(c, str) and c in names for c in cols):
            uniques.add((spec["asset"], tuple(cols)))
            continue
        if a.get("type") == "row_predicate":
            # `col in [...]` at the top of the condition: measure the column's values, so a list that is
            # really a downstream filter can be told apart from the domain (judged by the validator)
            cond = a.get("condition") or {}
            for leaf in (cond.get("clauses") or []) if cond.get("op") == "and" else [cond]:
                if not isinstance(leaf, dict):
                    continue
                if leaf.get("op") == "implies" and (leaf.get("then") or {}).get("op") == "in":
                    # implies(if: X, then: col in [...]): the list is judged only on the rows where X holds
                    then_left = leaf["then"].get("left") or {}
                    if then_left.get("column") in names and "function" not in then_left and isinstance(leaf.get("if"), dict):
                        in_lists.add((spec["asset"], then_left["column"], json.dumps(leaf["if"], sort_keys=True)))
                    continue
                col = (leaf.get("left") or {}).get("column")
                if leaf.get("op") == "in" and col in names and "function" not in (leaf.get("left") or {}):
                    in_lists.add((spec["asset"], col, None))
            # `col is not null` at the top: measure its NULL rate (a mostly-NULL column is optional by design)
            for leaf in (cond.get("clauses") or []) if cond.get("op") == "and" else [cond]:
                col = (leaf.get("left") or {}).get("column") if isinstance(leaf, dict) else None
                if isinstance(leaf, dict) and leaf.get("op") == "is_not_null" and col in names:
                    not_nulls.add((spec["asset"], col))
            continue
        if a.get("type") == "cross_asset_column_equality":
            # comparing values needs a key that identifies the rows of the reference table, or the join multiplies rows
            try:
                right = tuple(r for _, r in join_pairs(a))
            except (TypeError, IndexError):
                right = ()
            tabs = a.get("tables") or []
            if len(tabs) == 2 and right and all(isinstance(c, str) for c in right):
                ref_names = {c["name"] for c in asset_contexts.get(tabs[1], {}).get("columns", [])}
                if set(right) <= ref_names:
                    uniques.add((tabs[1], right))
        if a.get("type") not in ("cross_asset_missing_rows", "cross_asset_column_equality"):
            continue
        try:
            cols = {c for pair in join_pairs(a) for c in pair if isinstance(c, str)}
        except (TypeError, IndexError):
            continue
        for t in a.get("tables") or []:
            names = {c["name"] for c in asset_contexts.get(t, {}).get("columns", [])}
            wanted |= {(t, c) for c in cols if c in names}
        # a key mixing dates with other columns: measure whether the non-date part identifies rows at all
        tabs = a.get("tables") or []
        if len(tabs) == 2:
            t0_types = {c["name"]: str(c.get("type", "")) for c in asset_contexts.get(tabs[0], {}).get("columns", [])}
            try:
                left = [l for l, _ in join_pairs(a)]
            except (TypeError, IndexError):
                left = []
            rest = tuple(c for c in left if isinstance(c, str) and c in t0_types and "Date" not in t0_types[c])
            if rest and len(rest) < len(left):
                uniques.add((tabs[0], rest))
    if not wanted and not uniques and not in_lists and not not_nulls:
        return
    for table, col in sorted(wanted):
        key = f"join_key_stats:{table}:{col}"
        if key in profiling_summary:
            continue
        try:
            total, nulls = wh.query(f"SELECT count(), countIf(isNull(`{col}`)) FROM {table}")[1][0]
        except Exception as e:
            print(f"  join key stats for {table}.{col}: could not measure ({e})")
            continue
        profiling_summary[key] = {"dimension": "join_key_stats", "table": table, "column": col,
                                  "check_type": "join_key_stats", "rows": [{"total_rows": total, "nulls": nulls}],
                                  "truncated": False}
    # every proposed `unique` measured on exactly its columns (the profile only covers the column
    # sets the Dimension Selector named): read by the validator as a composite_key duplicate count
    for table, cols in sorted(uniques):
        key = f"uniqueness:{table}:{','.join(cols)}:measured"
        if key in profiling_summary:
            continue
        try:
            total, distinct = wh.query(
                f"SELECT count(), uniqExact(tuple({', '.join(f'`{c}`' for c in cols)})) FROM {table}")[1][0]
        except Exception as e:
            print(f"  uniqueness stats for {table}{list(cols)}: could not measure ({e})")
            continue
        profiling_summary[key] = {"dimension": "uniqueness", "table": table, "column": ",".join(cols),
                                  "check_type": "composite_key", "truncated": False,
                                  "rows": [{"total_rows": total, "composite_duplicate_count": total - distinct}]}
    for table, col in sorted(not_nulls):
        key = f"completeness:{table}:{col}:measured"
        if key in profiling_summary:
            continue
        try:
            total, nulls = wh.query(f"SELECT count(), countIf(isNull(`{col}`)) FROM {table}")[1][0]
        except Exception as e:
            print(f"  null rate for {table}.{col}: could not measure ({e})")
            continue
        profiling_summary[key] = {"dimension": "completeness", "table": table, "column": col, "check_type": "",
                                  "truncated": False, "rows": [{"total_rows": total, "nulls": nulls}]}
    for table, col, when in sorted(in_lists, key=str):
        key = f"validity:{table}:{col}:measured" + (f":when:{when}" if when else "")
        types = {c["name"]: str(c.get("type", "")) for c in asset_contexts.get(table, {}).get("columns", [])}
        if key in profiling_summary or types.get(col, "").startswith("Array("):
            continue
        try:
            where = f"WHERE {_render_condition(json.loads(when))} " if when else ""
            rows = wh.query(f"SELECT `{col}` AS value, count() AS occurrences, round(100 * count() / sum(count()) OVER (), 2) "
                                f"AS pct_of_total FROM {table} {where}GROUP BY value ORDER BY occurrences DESC LIMIT 50")[1]
        except Exception as e:
            print(f"  value distribution for {table}.{col}: could not measure ({e})")
            continue
        profiling_summary[key] = {"dimension": "validity", "table": table, "column": col, "check_type": "", "truncated": len(rows) == 50,
                                  "rows": [{"value": v, "occurrences": n, "pct_of_total": float(p)} for v, n, p in rows],
                                  **({"when": json.loads(when)} if when else {})}


def build_profiling_summary(profiling_results, max_rows_per_check: int = 8) -> dict:
    """
    Condenses the raw per-row profiling results captured by
    run_profiling() into a compact, prompt-friendly
    structure: one entry per check_id, with up to `max_rows_per_check` rows
    of metrics. Most profiling queries return exactly one row (completeness,
    uniqueness, freshness, accuracy, cardinality...); a few GROUP BY a
    column and can return many (Validity's frequency table, Consistency's
    co-occurrence table) -- those get capped here so the Test Specifier
    prompt doesn't balloon in size, with `truncated: true` marking that more
    rows existed than shown.
    """
    summary = {}
    for row in profiling_results:
        check_id = row["check_id"]
        entry = summary.setdefault(check_id, {
            "dimension": row["dimension"],
            "table": row["table_name"],
            "column": row["column_name"],
            "check_type": row["check_type"],
            "rows": [],
            "truncated": False,
        })
        if len(entry["rows"]) < max_rows_per_check:
            entry["rows"].append(row["metrics"])
        else:
            entry["truncated"] = True
    return summary


def _sanitize_column_name(name: str) -> str:
    """Turns a result column name into a safe metric key: letters, digits, underscores, not starting with a digit."""
    safe = re.sub(r'\W+', '_', str(name)).strip('_')
    if not safe:
        safe = "metric"
    if safe[0].isdigit():
        safe = f"m_{safe}"
    return safe


def render_script(sql_text: str, render) -> str:
    """A "-- CHECK_ID:" script as the database runs it: each block keeps its leading comment lines and its
    query goes through `render` (the connection's own SQL). A block that cannot be rendered is kept as it is."""
    blocks = []
    for block in re.split(r"(?=-- CHECK_ID:)", sql_text):
        lines = block.rstrip("\n").split("\n")
        n = next((i for i, line in enumerate(lines) if not line.lstrip().startswith("--")), len(lines))
        query = "\n".join(lines[n:]).strip()
        if query.endswith(";"):
            try:
                lines = lines[:n] + [render(query).rstrip(";") + ";"]
            except ValueError:
                pass
        blocks.append("\n".join(lines))
    return "\n\n".join(b for b in blocks if b.strip()) + "\n"


def select_checks(sql_text: str, check_ids) -> str:
    """The "-- CHECK_ID: <id>" blocks of a script whose id is in `check_ids`, in the same format."""
    blocks = [b for b in re.split(r"(?=-- CHECK_ID:)", sql_text) if b.startswith("-- CHECK_ID:")]
    return "".join(b for b in blocks if b.split("\n", 1)[0][len("-- CHECK_ID:"):].strip() in check_ids)


def run_profiling(wh, sql_text: str, progress=None) -> tuple[list[dict], list[dict]]:
    """
    Runs every "-- CHECK_ID: <id>" block of the profiling SQL (SELECT only, nothing is written anywhere) and
    returns (profiling_results, errors). Each result row is {"check_id", "dimension", "table_name",
    "column_name", "check_type", "metrics"}, one per row the query returned. A query that fails is recorded in
    `errors` as {"check_id", "error"} and the remaining checks still run.
    """
    progress = progress or (lambda message: None)
    profiling_results, errors = [], []
    blocks = [b for b in re.split(r'-- CHECK_ID:', sql_text) if b.strip()]
    for i, block in enumerate(blocks, 1):
        progress(f"Profiling: query {i} of {len(blocks)}")
        lines = block.strip().split('\n')
        check_id = lines[0].strip()
        sql_query = "\n".join(lines[1:]).strip()
        if not sql_query.endswith(';'):
            print(f"Warning: Skipping malformed query block for check_id '{check_id}' (missing semicolon).")
            continue
        parsed = parse_check_id(check_id)
        if parsed["dimension"].strip().lower() not in DQ_DIMENSIONS:
            errors.append({"check_id": check_id, "error": f"unknown dimension '{parsed['dimension']}'"})
            continue
        try:
            print(f"Executing check: {check_id}")
            columns, rows = wh.query(sql_query)
        except Exception as e:
            print(f"ERROR: check '{check_id}' failed: {e}")
            errors.append({"check_id": check_id, "error": str(e)})
            continue
        for row in rows:
            profiling_results.append({
                "check_id": check_id,
                "dimension": parsed["dimension"],
                "table_name": parsed["table_name"],
                "column_name": parsed["column_name"],
                "check_type": parsed["check_type"],
                "metrics": {_sanitize_column_name(k): v for k, v in zip(columns, row)},
            })
    print(f"Profiling: {len(profiling_results)} result row(s), {len(errors)} failed check(s).")
    return profiling_results, errors
