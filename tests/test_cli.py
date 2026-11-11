import json

from fake_llm import FakeLLM

from conftest import EXAMPLE
from dqvouch.cli import main, os_schedule_lines
from dqvouch.config import load_settings
from dqvouch.llm import QuotaExhaustedError
from dqvouch.pipeline import stage_review
from dqvouch.store import STAGES, Run

MINIMAL = """
[database]
engine = "clickhouse"
host = "localhost"
port = 1
schemas = ["landing"]
"""


def demo_config(tmp_path, demo_config_path):
    """The example's config with absolute sql_dir and a runs folder inside tmp_path."""
    text = demo_config_path.read_text(encoding="utf-8")
    text = text.replace('sql_dir = "sql"', f"sql_dir = '{(EXAMPLE / 'sql').as_posix()}'")
    cfg = tmp_path / "dqvouch.toml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_missing_api_key_exits_2(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("dqvouch.config.load_dotenv", lambda *a, **k: None)
    cfg = tmp_path / "dqvouch.toml"
    cfg.write_text(MINIMAL, encoding="utf-8")
    assert main(["run", "--config", str(cfg), "--no-review"]) == 2


def test_two_levels_make_two_complete_runs(tmp_path, demo_config_path, wh):
    cfg = demo_config(tmp_path, demo_config_path)
    assert main(["run", "--config", str(cfg), "--level", "C1", "C3", "--no-review"], llm=FakeLLM()) == 0
    runs = sorted((tmp_path / "runs").iterdir())
    assert len(runs) == 2
    for path in runs:
        assert Run.open(path).completed() == list(STAGES)
    by_level = {Run.open(p).info()["settings"]["context_level"]: Run.open(p) for p in runs}
    assert "skipped" in by_level["C1"].load("sql_rules")
    assert "skipped" not in by_level["C3"].load("sql_rules")


def test_quota_exhausted_exits_2(tmp_path, demo_config_path, wh):
    def llm(prompt):
        raise QuotaExhaustedError("daily quota")

    assert main(["run", "--config", str(demo_config(tmp_path, demo_config_path)), "--no-review"], llm=llm) == 2


def test_ui_binds_localhost(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.update(kwargs))
    assert main(["ui", "--no-browser", "--runs-dir", str(tmp_path / "runs")]) == 0
    assert calls["host"] == "127.0.0.1"


def _env(tmp_path):
    env = tmp_path / "test.env"
    env.write_text("DQVOUCH_DB_PASSWORD=dqvouch\n", encoding="utf-8")
    return env


def _reviewed_run_dir(tmp_path, demo_config_path, passing=False):
    """A demo run made with `dqvouch run --no-review`; `passing` re-reviews it with limits nothing exceeds."""
    assert main(["run", "--config", str(demo_config(tmp_path, demo_config_path)), "--no-review"], llm=FakeLLM()) == 0
    path = next((tmp_path / "runs").iterdir())
    if passing:
        run = Run.open(path)
        loose = {"metric": "failed_rows", "operator": "less_than_or_equal", "value": 1000000}
        stage_review(run, [{**s, "threshold": loose} for s in run.load("tests")["test_specifications"]], [])
    return path


def test_rerun_runs_the_confirmed_tests_and_exits_1_on_a_failure(tmp_path, demo_config_path, wh, monkeypatch, capsys):
    monkeypatch.delenv("DQVOUCH_DB_PASSWORD", raising=False)
    path = _reviewed_run_dir(tmp_path, demo_config_path)
    capsys.readouterr()
    assert main(["rerun", str(path), "--env-file", str(_env(tmp_path))]) == 1
    latest = Run.open(path).history()[0]
    assert latest["trigger"] == "command" and latest["counts"]["fail"] == 2
    assert "orders_trans_order_id_unique" in capsys.readouterr().out


def test_rerun_exits_0_when_nothing_fails(tmp_path, demo_config_path, wh, monkeypatch):
    monkeypatch.delenv("DQVOUCH_DB_PASSWORD", raising=False)
    path = _reviewed_run_dir(tmp_path, demo_config_path, passing=True)
    assert main(["rerun", str(path), "--env-file", str(_env(tmp_path))]) == 0


def test_rerun_exits_2_when_it_cannot_run(tmp_path, demo_config_path, wh, monkeypatch):
    monkeypatch.delenv("DQVOUCH_DB_PASSWORD", raising=False)
    env = str(_env(tmp_path))
    assert main(["rerun", str(tmp_path / "no-such-run"), "--env-file", env]) == 2
    unreviewed = Run.create(tmp_path / "other", load_settings(demo_config(tmp_path, demo_config_path)))
    assert main(["rerun", str(unreviewed.path), "--env-file", env]) == 2
    path = _reviewed_run_dir(tmp_path, demo_config_path)
    with Run.open(path).execution_lock():
        assert main(["rerun", str(path), "--env-file", env]) == 2
    info = json.loads((path / "run.json").read_text(encoding="utf-8"))
    info["settings"]["database"]["port"] = 1
    (path / "run.json").write_text(json.dumps(info), encoding="utf-8")
    assert main(["rerun", str(path), "--env-file", env]) == 2
    latest = Run.open(path).history()[0]
    assert latest["trigger"] == "command" and "cannot connect" in latest["error"]  # the alerts need to know


def test_os_schedule_lines_quote_paths_with_spaces():
    lines = os_schedule_lines(r"C:\Program Files\dq\dqvouch.exe", r"C:\My runs\r1", r"C:\My runs\.env", 15)
    assert r'/tr "\"C:\Program Files\dq\dqvouch.exe\" rerun \"C:\My runs\r1\" --env-file \"C:\My runs\.env\""' \
        in lines["schtasks"]
    assert "/sc minute /mo 15" in lines["schtasks"]
    assert lines["cron"].startswith("*/15 * * * * ")
    assert r"'C:\My runs\r1'" in lines["cron"]
    assert os_schedule_lines("/opt/dq", "/runs/it's", "/e", 120)["cron"].startswith("0 */2 * * * ")
    assert r"'/runs/it'\''s'" in os_schedule_lines("/opt/dq", "/runs/it's", "/e", 120)["cron"]
    two_days = os_schedule_lines("/opt/dq", "/r", "/e", 2880)
    assert two_days["cron"].startswith("0 0 */2 * * ") and "/sc daily /mo 2" in two_days["schtasks"]
