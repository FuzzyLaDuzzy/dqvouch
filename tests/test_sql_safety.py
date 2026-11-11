"""Text the LLM writes (ids, titles, rationales, metric names) must never reach an executable SQL line."""
from dqvouch.profiling import generate_sql_from_dq_json
from dqvouch.render_sql import generate_sql_from_test_specifications

ESCAPE = "\nSET default_transaction_read_only = off;\r\nDROP TABLE x;"


def _executable_lines(sql: str) -> list[str]:
    return [line for line in sql.split("\n") if line.strip() and not line.lstrip().startswith("--")]


def test_llm_text_in_test_headers_stays_in_comments():
    spec = {"id": "t" + ESCAPE, "asset": "db.t", "title": "title" + ESCAPE, "rationale": "why" + ESCAPE,
            "status": "proposed", "evidence": [{"path": "p" + ESCAPE}],
            "assertion": {"type": "unique", "columns": ["a"]},
            "threshold": {"metric": "failed_rows", "operator": "equals" + ESCAPE, "value": 0}}
    lines = _executable_lines(generate_sql_from_test_specifications([spec]))
    assert lines == ["SELECT count() - uniqExact(a) AS failed_rows FROM db.t;"]


def test_metric_name_must_be_an_identifier():
    spec = {"id": "t", "asset": "db.t", "assertion": {"type": "unique", "columns": ["a"]},
            "threshold": {"metric": "x FROM db.t; SET default_transaction_read_only = off; SELECT 0 AS failed_rows",
                          "operator": "equals", "value": 0}}
    lines = _executable_lines(generate_sql_from_test_specifications([spec]))
    assert lines == ["SELECT count() - uniqExact(a) AS failed_rows FROM db.t;"]


def test_llm_rationale_in_profiling_stays_in_comments():
    dims = {"tables": [{"table": "db.t", "dimensions": [
        {"dimension": "Uniqueness", "columns": ["a"], "rationale": "why" + ESCAPE}]}],
        "cross_table_checks": [{"dimension": "Consistency", "tables_involved": ["db.t", "db.u"], "columns_involved": ["a"],
                                "rationale": "why" + ESCAPE, "join_key": "a", "shared_columns": ["a"],
                                "join_key_identifies_rows": True}]}
    ctx = {t: {"columns": [{"name": "a", "type": "String"}]} for t in ("db.t", "db.u")}
    sql = generate_sql_from_dq_json(dims, ctx)
    assert not any(k in line for line in _executable_lines(sql) for k in ("SET ", "DROP "))
