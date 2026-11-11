"""The same pipeline on every engine: prompts name the engine, every generated query runs, the SQL shown is the SQL run."""
from fake_llm import FakeLLM

from dqvouch.context import build_asset_contexts
from dqvouch.pipeline import (
    stage_context, stage_dimensions, stage_profiling, stage_results, stage_review, stage_sql_rules, stage_tests,
)
from dqvouch.profiling import (
    add_join_key_stats, build_profiling_summary, choose_cross_table_join_keys, generate_sql_from_dq_json,
    run_profiling,
)
from dqvouch.render_sql import generate_sql_from_test_specifications, run_tests
from dqvouch.selector import build_dimension_selector_prompt
from dqvouch.signals import add_downstream_usage_and_signals
from dqvouch.specifier import build_test_specifier_prompt
from dqvouch.store import Run

ZERO = {"metric": "failed_rows", "operator": "equals", "value": 0}


def test_prompts_name_the_engine():
    assert "a set of ClickHouse table schemas" in build_dimension_selector_prompt("{}", "none")
    assert "a set of PostgreSQL table schemas" in build_dimension_selector_prompt("{}", "none", engine="PostgreSQL")
    prompt = build_test_specifier_prompt({"db.t": {"asset": {"name": "db.t"}, "columns": []}},
                                         {"tables": [], "cross_table_checks": []}, None, engine="PostgreSQL")
    assert "a set of PostgreSQL tables" in prompt


def test_case_fallback_is_a_signal():
    assets = {
        "db.s": {"columns": [{"name": "status", "type": "String"}], "lineage": {"upstream_assets": "unknown"},
                 "transformation": {"raw_sql": "unknown"}},
        "db.f": {"columns": [{"name": "status", "type": "String"}], "lineage": {"upstream_assets": ["db.s"]},
                 "transformation": {"source_file": "f.sql", "raw_sql":
                     "INSERT INTO db.f SELECT CASE WHEN status = '' THEN 'unknown' ELSE status END AS status FROM db.s"}},
    }
    add_downstream_usage_and_signals(assets, with_sql=True)
    assert assets["db.f"]["dq_signals"]["fallback_value_status"]["fallback"] == "unknown"


def _dimensions():
    dims = {"Completeness": ["order_id", "status", "customer_email"], "Uniqueness": ["order_id", "status"],
            "Freshness": ["order_date"], "Validity": ["status"], "Accuracy": ["quantity", "unit_price"],
            "Consistency": ["status", "quantity"]}
    return {
        "tables": [{"table": "landing.orders_raw", "dimensions": [
            {"dimension": d, "columns": cols, "rationale": "r"} for d, cols in dims.items()]}],
        "cross_table_checks": [{"dimension": "Consistency", "tables_involved": ["landing.orders_raw", "staging.orders_trans"],
                                "columns_involved": ["order_id", "status"], "rationale": "r"}],
    }


def _specs():
    col = lambda c: {"column": c}  # noqa: E731
    fn = lambda f, c: {"function": f, "column": c}  # noqa: E731
    predicate = {"op": "and", "clauses": [
        {"op": "gte", "left": fn("length", "status"), "right": {"value": 0}},
        {"op": "neq", "left": fn("lower", "status"), "right": {"value": "x"}},
        {"op": "neq", "left": fn("trim", "status"), "right": {"value": "x"}},
        {"op": "neq", "left": fn("domain", "customer_email"), "right": {"value": "x"}},
        {"op": "lte", "left": col("order_date"), "right": {"function": "today"}},
        {"op": "implies", "if": {"op": "eq", "left": col("status"), "right": {"value": "test"}},
         "then": {"op": "is_not_null", "left": col("order_id")}},
    ]}
    raw, trans = "landing.orders_raw", "staging.orders_trans"
    return [
        {"id": "predicate", "asset": raw, "assertion": {"type": "row_predicate", "condition": predicate},
         "threshold": ZERO},
        {"id": "unique", "asset": raw, "assertion": {"type": "unique", "columns": ["order_id", "status"]},
         "threshold": ZERO},
        {"id": "row_count", "asset": trans, "assertion": {"type": "cross_asset_row_count", "tables": [raw, trans]}},
        {"id": "missing", "asset": trans, "assertion": {
            "type": "cross_asset_missing_rows", "tables": [trans, raw],
            "join_key": [["order_id", "order_id"], ["status", "status"]],
            "filters": {raw: {"op": "neq", "left": col("status"), "right": {"value": "test"}}}}, "threshold": ZERO},
        {"id": "equality", "asset": trans, "assertion": {
            "type": "cross_asset_column_equality", "tables": [trans, raw], "join_key": "order_id",
            "columns": ["status", "status"]}, "threshold": ZERO},
    ]


def test_every_generated_query_runs(demo_settings, wh):
    ctx = build_asset_contexts(wh.fetch_metadata(demo_settings.database.schemas), {}, "C2")
    dims = _dimensions()
    choose_cross_table_join_keys(dims, ctx, wh)
    results, errors = run_profiling(wh, generate_sql_from_dq_json(dims, ctx))
    assert errors == []
    assert results

    specs = _specs()
    rows = run_tests(wh, generate_sql_from_test_specifications(specs), {s["id"]: s for s in specs})
    assert {r["check_id"] for r in rows} == {s["id"] for s in specs}
    assert [(r["check_id"], r["error"]) for r in rows if r["verdict"] == "error"] == []

    summary = build_profiling_summary(results)
    add_join_key_stats(summary, specs, ctx, wh)
    assert any(k.startswith("join_key_stats:") for k in summary)


def test_displayed_sql_is_the_engine_sql(tmp_path, demo_settings, wh):
    demo_settings.runs_dir = str(tmp_path)
    run, llm = Run.create(tmp_path, demo_settings), FakeLLM()
    stage_context(run, wh)
    stage_dimensions(run, llm)
    stage_profiling(run, wh)
    stage_sql_rules(run, llm)
    t = stage_tests(run, llm, wh)
    stage_review(run, t["test_specifications"], t["metric_thresholds"])
    rows = stage_results(run, wh)["rows"]
    sql_by_test = {r["check_id"].split("__")[-1]: r["sql"] for r in rows}
    if demo_settings.database.engine == "postgres":
        assert "countIf" not in run.load("profiling")["sql"]
        assert "uniqExact" not in run.load("results")["sql"]
        assert all("uniqExact" not in sql for sql in sql_by_test.values())
    else:
        assert "uniqExact" in sql_by_test["orders_trans_order_id_unique"]


def test_render_script_keeps_clickhouse_sql_byte_for_byte():
    from dqvouch.profiling import render_script
    from dqvouch.translate import to_postgres
    sql = generate_sql_from_test_specifications(_specs())
    assert render_script(sql, lambda q: q) == sql
    rendered = render_script(sql, to_postgres)
    assert rendered.count("-- CHECK_ID:") == sql.count("-- CHECK_ID:")
    assert "uniqExact" in sql and "uniqExact" not in rendered


# What ClickHouse 24.8's domain() returns: the reference the PostgreSQL translation must match.
DOMAINS = {
    "user@example.com": "example.com", "x.com/path": "x.com", "": "", "unknown": "", "N/A": "", "-": "",
    "12345": "", "john.doe@": "", "john doe@example.com": "", "exa mple.com": "", "https://": "",
    "https://www.x.com/a?b": "www.x.com", "http://x.com:8080/p": "x.com", "sub.domain.co.uk": "sub.domain.co.uk",
    "mailto:a@b.com": "", "//cdn.x.com/a": "cdn.x.com",
}


def test_domain_matches_clickhouse(wh):
    values = list(DOMAINS)
    sql = "SELECT " + ", ".join(f"domain('{v}') AS d{i}" for i, v in enumerate(values))
    assert dict(zip(values, wh.query(sql)[1][0])) == DOMAINS


def test_empty_input_counts_zero(wh):
    # countIf over no rows is 0 on ClickHouse; a ratio over no rows is not a number (nan there, NULL here).
    _, rows = wh.query("SELECT countIf(a IS NULL) AS n, countIf(a IS NULL) / count() AS r, "
                       "round(countIf(a IS NULL) / count() * 100, 2) AS pct FROM (SELECT 1 AS a WHERE 1 = 0)")
    n, r, pct = rows[0]
    assert n == 0
    assert all(v is None or v != v for v in (r, pct))  # None or nan


def test_boolean_literals_compare_on_both_engines(wh):
    from dqvouch.render_sql import _sql_literal
    t, f = _sql_literal(True), _sql_literal(False)
    sql = f"SELECT countIf(x = {t}) AS a, countIf(x IN ({t}, {f})) AS b, countIf(x != {f}) AS c FROM (SELECT 1 = 1 AS x)"
    assert wh.query(sql)[1] == [(1, 1, 1)]


def test_every_renderer_observes_the_same_value_on_both_engines(wh):
    """The orders example's planted defects, counted by each assertion type (NULL ids, duplicate ids, typos, rows
    that never reached staging). Both engines must observe exactly these numbers."""
    raw, trans, fact = "landing.orders_raw", "staging.orders_trans", "analytic.fact_orders"
    col = lambda c: {"column": c}  # noqa: E731
    expected = {
        "unique_id": ({"type": "unique", "columns": ["order_id"]}, raw, 5),               # 3 repeated ids + 2 NULLs
        "unique_id_status": ({"type": "unique", "columns": ["order_id", "status"]}, raw, 1),
        "id_not_null": ({"type": "row_predicate", "condition": {"op": "is_not_null", "left": col("order_id")}}, raw, 2),
        "status_known": ({"type": "row_predicate", "condition": {
            "op": "in", "left": col("status"), "values": ["pending", "shipped", "delivered", "cancelled"]}}, raw, 9),
        "email_domain": ({"type": "row_predicate", "condition": {
            "op": "eq", "left": {"function": "domain", "column": "customer_email"}, "right": {"value": "example.com"}}},
            raw, 0),
        "row_count": ({"type": "cross_asset_row_count", "tables": [raw, trans]}, trans, 7),
        "missing": ({"type": "cross_asset_missing_rows", "tables": [raw, trans], "join_key": "order_id"}, raw, 7),
        "missing_filtered": ({"type": "cross_asset_missing_rows", "tables": [trans, fact], "join_key": "order_id",
                              "filters": {trans: {"op": "neq", "left": col("status"), "right": {"value": "test"}}}},
                             fact, 3),                                                    # customer 999's orders
        "status_equal": ({"type": "cross_asset_column_equality", "tables": [trans, fact], "join_key": "order_id",
                          "columns": ["status", "status"]}, fact, 9),
        "return_unique": ({"type": "unique", "columns": ["return_id"]}, "staging.returns", 1),  # id 7 in both sources
        "return_of_known_order": ({"type": "cross_asset_missing_rows", "tables": ["staging.returns", trans],
                                   "join_key": "order_id"}, "staging.returns", 1),          # the return of order 5000
        "customer_unique": ({"type": "unique", "columns": ["customer_id"]}, "staging.customers", 0),  # ROW_NUMBER dedup
        "country_valid": ({"type": "row_predicate", "condition": {
            "op": "in", "left": col("country"), "values": ["PT", "ES", "FR", "DE", "IT"]}}, "staging.customers", 2),
        "email_has_domain": ({"type": "row_predicate", "condition": {
            "op": "neq", "left": {"function": "domain", "column": "email"}, "right": {"value": ""}}},
            "staging.customers", 1),                                                         # the empty e-mail
    }
    specs = [{"id": k, "asset": asset, "assertion": a} for k, (a, asset, _) in expected.items()]
    rows = run_tests(wh, generate_sql_from_test_specifications(specs), {s["id"]: s for s in specs})
    assert {r["check_id"]: r["observed"] for r in rows} == {k: v for k, (_, _, v) in expected.items()}
