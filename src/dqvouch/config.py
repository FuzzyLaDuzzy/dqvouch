"""Settings read from a TOML file. Secrets never leave this object: public_dict() is what gets written to disk."""
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, find_dotenv, load_dotenv, set_key, unset_key

ENGINES = ("clickhouse", "postgres")
ENGINE_LABELS = {"clickhouse": "ClickHouse", "postgres": "PostgreSQL"}
CONTEXT_LEVELS = ("C1", "C2", "C3")
PROVIDERS = ("gemini", "openai")  # openai: any server with OpenAI's chat completions API (Ollama, LM Studio...)


@dataclass
class DatabaseSettings:
    engine: str = "clickhouse"
    host: str = "localhost"
    port: int = 8123
    user: str = "default"
    password: str = ""
    schemas: list[str] = field(default_factory=list)
    database: str = ""  # PostgreSQL database name; ClickHouse ignores it
    query_timeout_seconds: int = 600  # longest a single query may run; 0 = no limit


@dataclass
class Settings:
    database: DatabaseSettings
    model: str = "gemini-3.5-flash-lite"
    provider: str = "gemini"
    base_url: str | None = None  # provider "openai" only, e.g. http://localhost:11434/v1 for Ollama
    sql_dir: str | None = None
    runs_dir: str = "runs"
    context_level: str = "C3"
    signals: bool = True
    sql_rules: bool = True
    repair: bool = True
    propose_on_change: bool = True  # a scheduled execution that finds a structural change proposes tests for it

    def public_dict(self) -> dict:
        """Everything except the database password."""
        data = asdict(self)
        data["database"].pop("password")
        return data

    def validate(self) -> None:
        if self.database.engine not in ENGINES:
            raise ValueError(f"database.engine must be one of {ENGINES}, got {self.database.engine!r}")
        if self.database.engine == "postgres" and not self.database.database:
            raise ValueError("database.database (the PostgreSQL database name) is required for engine 'postgres'")
        schemas = self.database.schemas
        if not isinstance(schemas, list) or not schemas or not all(isinstance(s, str) and s for s in schemas):
            raise ValueError(f"database.schemas must be a non-empty list of names, got {schemas!r}")
        timeout = self.database.query_timeout_seconds
        if type(timeout) is not int or timeout < 0:
            raise ValueError(f"database.query_timeout_seconds must be a whole number of seconds, 0 or more, "
                             f"got {timeout!r}")
        if self.provider not in PROVIDERS:
            raise ValueError(f"llm.provider must be one of {PROVIDERS}, got {self.provider!r}")
        if self.provider == "openai" and not (isinstance(self.base_url, str) and self.base_url.strip()):
            raise ValueError("llm.base_url (the server's address, e.g. http://localhost:11434/v1) is required for "
                             "provider 'openai'")
        if self.context_level not in CONTEXT_LEVELS:
            raise ValueError(f"context_level must be one of {CONTEXT_LEVELS}, got {self.context_level!r}")
        if type(self.propose_on_change) is not bool:
            raise ValueError(f"run.propose_on_change must be true or false, got {self.propose_on_change!r}")


def load_settings(path: str | Path) -> Settings:
    """Reads [database], [llm] and [run] from a TOML file. sql_dir and runs_dir are relative to the file's folder;
    DQVOUCH_DB_PASSWORD in the environment replaces the password in the file."""
    path = Path(path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    base = path.resolve().parent
    run = raw.get("run", {})
    try:
        database = DatabaseSettings(**raw.get("database", {}))
    except TypeError as e:  # an unknown or misspelled key in [database]
        raise ValueError(f"invalid [database] section in {path}: {e}") from e
    database.password = os.getenv("DQVOUCH_DB_PASSWORD", database.password)
    sql_dir = run.get("sql_dir")
    llm = raw.get("llm", {})
    settings = Settings(
        database=database,
        model=llm.get("model", Settings.model),
        provider=llm.get("provider", Settings.provider),
        base_url=llm.get("base_url"),
        sql_dir=str(base / sql_dir) if sql_dir else None,
        runs_dir=str(base / run.get("runs_dir", "runs")),
        propose_on_change=run.get("propose_on_change", True),
    )
    settings.validate()
    return settings


def gemini_api_key() -> str | None:
    """GEMINI_API_KEY from the environment or a .env file in the working directory (or a parent of it)."""
    load_dotenv(find_dotenv(usecwd=True))
    return os.getenv("GEMINI_API_KEY")


def llm_api_key(provider: str) -> str | None:
    """The key for a provider from the environment or a .env file: GEMINI_API_KEY, or OPENAI_API_KEY (optional)."""
    load_dotenv(find_dotenv(usecwd=True))
    return os.getenv("OPENAI_API_KEY" if provider == "openai" else "GEMINI_API_KEY")


SECRET_NAMES = ("GEMINI_API_KEY", "OPENAI_API_KEY", "DQVOUCH_DB_PASSWORD", "DQVOUCH_ALERT_WEBHOOK_URL",
                "DQVOUCH_SMTP_PASSWORD")
# Settings kept in the same .env file that are not secret (shown back to the person).
PLAIN_NAMES = ("DQVOUCH_SMTP_HOST", "DQVOUCH_SMTP_PORT", "DQVOUCH_SMTP_USER", "DQVOUCH_SMTP_FROM", "DQVOUCH_SMTP_TO")
ALERT_NAMES = ("DQVOUCH_ALERT_WEBHOOK_URL", "DQVOUCH_SMTP_PASSWORD") + PLAIN_NAMES
_NAMES = SECRET_NAMES + PLAIN_NAMES


class Secrets:
    """The model keys, the database password and the alert settings of this installation. A value already in the
    environment when dqvouch starts wins; then the one saved in the .env file; for the password, the config file's
    (demo) value last. Values are only ever written to the .env file; status() says where each secret comes from,
    never what it is (the PLAIN_NAMES are not secret and plain_values() shows them)."""

    def __init__(self, env_path, defaults: dict | None = None):
        self.env_path = Path(env_path)
        saved = dotenv_values(self.env_path) if self.env_path.exists() else {}
        self._layers = (
            ("environment", {n: os.environ[n] for n in _NAMES if os.environ.get(n)}),
            ("env_file", {n: saved[n] for n in _NAMES if saved.get(n)}),
            ("config_file", {n: v for n, v in (defaults or {}).items() if n in SECRET_NAMES and v}),
        )

    @staticmethod
    def _check(name: str) -> None:
        if name not in _NAMES:
            raise KeyError(f"unknown secret {name!r}")

    def source(self, name: str) -> str | None:
        self._check(name)
        return next((src for src, values in self._layers if name in values), None)

    def get(self, name: str) -> str | None:
        self._check(name)
        return next((values[name] for _, values in self._layers if name in values), None)

    def save(self, name: str, value: str) -> None:
        self._check(name)
        if not self.env_path.exists():  # private from the start (it holds secrets); set_key keeps the mode
            os.close(os.open(self.env_path, os.O_CREAT | os.O_WRONLY, 0o600))
        set_key(str(self.env_path), name, value)
        self._layers[1][1][name] = value  # an environment value keeps priority

    def clear(self, name: str) -> None:
        """Removes a value from the .env file (one set in the environment still applies)."""
        self._check(name)
        if self.env_path.exists():
            unset_key(str(self.env_path), name)
        self._layers[1][1].pop(name, None)

    def status(self) -> dict:
        return {name: {"set": self.get(name) is not None, "source": self.source(name)} for name in SECRET_NAMES}

    def plain_values(self) -> dict:
        return {name: self.get(name) or "" for name in PLAIN_NAMES}

    def alert_values(self) -> dict:
        """Everything the alerts need, secrets included: for sending, never for showing."""
        return {name: self.get(name) or "" for name in ALERT_NAMES}

    def redact(self, text: str) -> str:
        """`text` with every secret's value replaced by ***."""
        for name in SECRET_NAMES:
            value = self.get(name)
            if value:
                text = text.replace(value, "***")
                parts = urlsplit(value)  # an error may quote only a URL's path or query, where a webhook's token is
                for piece in (parts.path, parts.query):
                    if len(piece) >= 8:
                        text = text.replace(piece, "***")
        return text
