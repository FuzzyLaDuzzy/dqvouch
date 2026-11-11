import pytest

from dqvouch.context import load_transformations_by_asset
from dqvouch.llm import GeminiLLM, QuotaExhaustedError
from dqvouch.selector import select_dimensions


def test_transformations_detect_ctas_and_views(tmp_path):
    (tmp_path / "a.sql").write_text("CREATE OR REPLACE VIEW analytic.v AS SELECT 1")
    (tmp_path / "b.sql").write_text("CREATE TABLE staging.t AS SELECT * FROM landing.raw")
    (tmp_path / "c.sql").write_text("CREATE MATERIALIZED VIEW analytic.mv TO analytic.target AS SELECT 1")
    (tmp_path / "d.sql").write_text("CREATE TABLE IF NOT EXISTS landing.raw (id UInt64) ENGINE = MergeTree ORDER BY id")
    m = load_transformations_by_asset(str(tmp_path))
    assert {"analytic.v", "staging.t", "analytic.target"} <= set(m)
    assert "landing.raw" not in m and "analytic.mv" not in m
    assert m["staging.t"]["source_file"] == "b.sql"


def test_quota_error_is_not_retried():
    calls = []

    class FakeModels:
        def generate_content(self, **kw):
            calls.append(1)
            raise Exception("429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerDayPerProjectPerModel")

    llm = GeminiLLM("m", api_key="k")
    llm._client = type("C", (), {"models": FakeModels()})()
    with pytest.raises(QuotaExhaustedError, match="daily quota"):
        llm("hi")
    assert calls == [1]


CTX = {"db.t": {"asset": {"name": "db.t"}, "columns": []}}


def test_select_dimensions_retries_once():
    answers = iter(["no json", '```json\n{"tables": [], "cross_table_checks": []}\n```'])
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return next(answers)

    assert select_dimensions(CTX, "C1", llm)[0] == {"tables": [], "cross_table_checks": []}
    assert len(calls) == 2


def test_select_dimensions_raises_after_second_bad_answer():
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return "no json"

    with pytest.raises(ValueError):
        select_dimensions(CTX, "C1", llm)
    assert len(calls) == 2
