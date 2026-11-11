import json
import os
from pathlib import Path

import pytest

from dqvouch.config import load_settings

VALID = """
[database]
engine = "clickhouse"
host = "localhost"
port = 18123
user = "u"
password = "x"
schemas = ["landing"]

[llm]
model = "gemini-3.5-flash-lite"

[run]
sql_dir = "sql"
"""


def write(tmp_path, text):
    path = tmp_path / "dqvouch.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_settings_env_password_and_relative_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DQVOUCH_DB_PASSWORD", "y")
    s = load_settings(write(tmp_path, VALID))
    assert s.database.password == "y"
    assert Path(s.sql_dir) == tmp_path / "sql"
    public = json.dumps(s.public_dict())
    assert '"x"' not in public and '"y"' not in public and "password" not in public


def test_load_settings_rejects_bad_values(tmp_path, monkeypatch):
    monkeypatch.delenv("DQVOUCH_DB_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="engine"):
        load_settings(write(tmp_path, VALID.replace('engine = "clickhouse"', 'engine = "oracle"')))
    with pytest.raises(ValueError, match="schemas"):
        load_settings(write(tmp_path, VALID.replace('schemas = ["landing"]', "schemas = []")))


def test_query_timeout_is_read_and_must_not_be_negative(tmp_path, monkeypatch):
    monkeypatch.delenv("DQVOUCH_DB_PASSWORD", raising=False)
    assert load_settings(write(tmp_path, VALID)).database.query_timeout_seconds == 600
    limited = VALID.replace('schemas = ["landing"]', 'schemas = ["landing"]\nquery_timeout_seconds = 30')
    assert load_settings(write(tmp_path, limited)).database.query_timeout_seconds == 30
    with pytest.raises(ValueError, match="query_timeout_seconds"):
        load_settings(write(tmp_path, limited.replace("= 30", "= -1")))


def test_openai_provider_needs_a_base_url(tmp_path, monkeypatch):
    monkeypatch.delenv("DQVOUCH_DB_PASSWORD", raising=False)
    assert load_settings(write(tmp_path, VALID)).provider == "gemini"
    local = VALID.replace("[llm]", '[llm]\nprovider = "openai"')
    with pytest.raises(ValueError, match="base_url"):
        load_settings(write(tmp_path, local))
    s = load_settings(write(tmp_path, local.replace('"openai"', '"openai"\nbase_url = "http://localhost:11434/v1"')))
    assert (s.provider, s.base_url) == ("openai", "http://localhost:11434/v1")
    with pytest.raises(ValueError, match="provider"):
        load_settings(write(tmp_path, VALID.replace("[llm]", '[llm]\nprovider = "mystery"')))


def test_plain_alert_settings_are_shown_and_secrets_are_not(tmp_path, monkeypatch):
    from dqvouch.config import Secrets
    for name in ("DQVOUCH_SMTP_HOST", "DQVOUCH_SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    secrets = Secrets(tmp_path / ".env")
    secrets.save("DQVOUCH_SMTP_HOST", "smtp.example")
    secrets.save("DQVOUCH_SMTP_PASSWORD", "pw-123")
    assert secrets.plain_values()["DQVOUCH_SMTP_HOST"] == "smtp.example"
    assert "pw-123" not in json.dumps(secrets.status()) + json.dumps(secrets.plain_values())
    assert secrets.status()["DQVOUCH_SMTP_PASSWORD"]["set"] is True
    assert secrets.redact("login pw-123 failed at smtp.example") == "login *** failed at smtp.example"
    assert secrets.alert_values()["DQVOUCH_SMTP_PASSWORD"] == "pw-123"
    secrets.clear("DQVOUCH_SMTP_HOST")
    assert Secrets(tmp_path / ".env").plain_values()["DQVOUCH_SMTP_HOST"] == ""


def test_a_partial_webhook_url_is_redacted_too(tmp_path, monkeypatch):
    from dqvouch.config import Secrets
    monkeypatch.delenv("DQVOUCH_ALERT_WEBHOOK_URL", raising=False)
    secrets = Secrets(tmp_path / ".env")
    secrets.save("DQVOUCH_ALERT_WEBHOOK_URL", "https://hooks.slack.com/services/T000/B000/s3cr3tToken")
    text = secrets.redact("URL can't contain control characters. '/services/T000/B000/s3cr3tToken x' (found at least ' ')")
    assert "s3cr3tToken" not in text and "***" in text


def test_gemini_api_key_reads_env_file_in_working_directory(tmp_path, monkeypatch):
    from dqvouch.config import gemini_api_key
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=from-cwd\n", encoding="utf-8")
    try:
        assert gemini_api_key() == "from-cwd"
    finally:
        os.environ.pop("GEMINI_API_KEY", None)  # load_dotenv writes os.environ; keep other tests independent


def test_postgres_requires_database(tmp_path, monkeypatch):
    monkeypatch.delenv("DQVOUCH_DB_PASSWORD", raising=False)
    with pytest.raises(ValueError, match=r"database\.database"):
        load_settings(write(tmp_path, VALID.replace('engine = "clickhouse"', 'engine = "postgres"')))


def test_secrets_environment_wins_and_file_is_written(tmp_path, monkeypatch):
    from dqvouch.config import Secrets
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    secrets = Secrets(env)
    secrets.save("GEMINI_API_KEY", "typed")
    assert "typed" in env.read_text(encoding="utf-8")
    assert secrets.get("GEMINI_API_KEY") == "from-env" and secrets.source("GEMINI_API_KEY") == "environment"


def test_secrets_saved_value_is_used_and_status_has_no_values(tmp_path, monkeypatch):
    from dqvouch.config import Secrets
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    secrets = Secrets(tmp_path / ".env")
    secrets.save("GEMINI_API_KEY", "sk-typed-1")
    assert secrets.get("GEMINI_API_KEY") == "sk-typed-1" and secrets.source("GEMINI_API_KEY") == "env_file"
    assert Secrets(tmp_path / ".env").get("GEMINI_API_KEY") == "sk-typed-1"  # read back from the file
    assert "sk-typed-1" not in json.dumps(secrets.status())
    assert secrets.status()["GEMINI_API_KEY"] == {"set": True, "source": "env_file"}


def test_config_file_password_is_the_fallback(tmp_path, monkeypatch):
    from dqvouch.config import Secrets
    monkeypatch.delenv("DQVOUCH_DB_PASSWORD", raising=False)
    secrets = Secrets(tmp_path / ".env", defaults={"DQVOUCH_DB_PASSWORD": "demo"})
    assert secrets.get("DQVOUCH_DB_PASSWORD") == "demo" and secrets.source("DQVOUCH_DB_PASSWORD") == "config_file"


def test_unknown_secret_name_is_refused(tmp_path):
    from dqvouch.config import Secrets
    with pytest.raises(KeyError):
        Secrets(tmp_path / ".env").save("PATH", "x")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_saved_secrets_file_is_private(tmp_path, monkeypatch):
    from dqvouch.config import Secrets
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    Secrets(tmp_path / ".env").save("GEMINI_API_KEY", "k")
    assert (tmp_path / ".env").stat().st_mode & 0o077 == 0  # only the owner can read the key and the password
