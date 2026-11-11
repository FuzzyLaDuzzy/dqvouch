"""Export the confirmed tests as one SQL file, in the run's own database dialect, to run anywhere without dqvouch."""
import re

import pytest
from fake_llm import FakeLLM

from dqvouch.cli import main
from dqvouch.pipeline import (
    export_sql, stage_context, stage_dimensions, stage_profiling, stage_review, stage_sql_rules, stage_tests,
)
from dqvouch.store import Run


def reviewed_run(tmp_path, demo_settings, wh, exclude=("fact_orders_status_domain",)):
    """A demo run whose review excludes `exclude` and adds two metric thresholds: one on a profiling check and one
    on a value the tests stage measured itself."""
    demo_settings.runs_dir = str(tmp_path)
    run, llm = Run.create(tmp_path, demo_settings), FakeLLM()
    stage_context(run, wh)
    stage_dimensions(run, llm)
    stage_profiling(run, wh)
    stage_sql_rules(run, llm)
    t = stage_tests(run, llm, wh)
    first = run.load("profiling")["profiling_results"][0]
    summary = t["profiling_summary"]
    measured = next(k for k in summary if k.startswith("join_key_stats:") or k.endswith(":measured"))
    thresholds = [
        {"id": "mt_profile", "asset": first["check_id"].split(":")[1], "check_id": first["check_id"],
         "metric": next(iter(first["metrics"])), "operator": "greater_than_or_equal", "value": 0, "status": "confirmed"},
        {"id": "mt_measured", "asset": summary[measured]["table"], "check_id": measured,
         "metric": next(iter(summary[measured]["rows"][0])), "operator": "greater_than_or_equal", "value": 0,
         "status": "confirmed"},
    ]
    specs = [{**s, "status": "excluded" if s["id"].split("__")[-1] in exclude else "confirmed"}
             for s in t["test_specifications"]]
    stage_review(run, specs, thresholds)
    return run


def queries(script: str) -> list[str]:
    blocks = re.split(r"(?=-- CHECK_ID:)", script)
    found = []
    for block in blocks[1:]:
        body = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("--")).strip()
        if body:
            found.append(body.rstrip(";"))
    return found


def test_the_export_has_one_query_per_confirmed_test(tmp_path, demo_settings, wh):
    run = reviewed_run(tmp_path, demo_settings, wh)
    script = export_sql(run)
    confirmed = run.load("review")["test_specifications"]
    assert script.count("-- CHECK_ID:") == len(confirmed) + 1  # + the threshold on a profiling check
    assert "fact_orders_status_domain" not in script
    assert all(s["id"] in script for s in confirmed)
    assert "mt_measured" in script and "no standalone query" in script
    assert run.id in script.splitlines()[0]


def test_every_exported_query_runs_on_the_database(tmp_path, demo_settings, wh):
    run = reviewed_run(tmp_path, demo_settings, wh)
    found = queries(export_sql(run))
    assert len(found) == 2
    for sql in found:
        if demo_settings.database.engine == "postgres":
            wh._conn.execute(sql).fetchall()  # already PostgreSQL: run it as it is, not translated again
        else:
            wh.query(sql)


def test_postgres_export_is_postgres_sql(tmp_path, demo_settings, wh):
    if demo_settings.database.engine != "postgres":
        pytest.skip("the ClickHouse export is dqvouch's own SQL")
    script = "\n".join(queries(export_sql(reviewed_run(tmp_path, demo_settings, wh))))
    assert "`" not in script and "countIf" not in script


def test_export_needs_a_review(tmp_path, demo_settings, wh, capsys):
    demo_settings.runs_dir = str(tmp_path)
    run = Run.create(tmp_path, demo_settings)
    with pytest.raises(ValueError, match="review"):
        export_sql(run)
    assert main(["export", str(run.path)]) == 2
    reviewed = reviewed_run(tmp_path / "other", demo_settings, wh)
    out = tmp_path / "tests.sql"
    assert main(["export", str(reviewed.path), "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == export_sql(reviewed)
