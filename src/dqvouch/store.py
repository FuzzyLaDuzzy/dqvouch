"""One run = one folder: run.json (settings without secrets) plus one NN_<stage>.json per finished stage.
Saving a stage deletes the later ones, so going back and redoing a stage never mixes old and new results."""
import contextlib
import datetime
import decimal
import json
import os
from pathlib import Path

from dqvouch.config import Settings

STAGES = ("context", "dimensions", "profiling", "sql_rules", "tests", "review", "results")
_MISSING = object()
LOCK_STALE_SECONDS = 24 * 3600  # a lock left by a killed process blocks for a day at most


class RunBusyError(RuntimeError):
    """Another execution of the run is in progress, in this process or in another one (`dqvouch rerun`)."""


def _json_default(o):
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()
    return str(o)


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, default=_json_default, ensure_ascii=False), encoding="utf-8")


class Run:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.id = self.path.name

    @classmethod
    def create(cls, runs_dir, settings: Settings | dict) -> "Run":
        """A new run folder. `settings` is a Settings, or a dict that is already public (an earlier run's)."""
        runs_dir = Path(runs_dir)
        runs_dir.mkdir(parents=True, exist_ok=True)
        base = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        path, n = runs_dir / base, 1
        while True:
            try:
                path.mkdir()
                break
            except FileExistsError:
                n += 1
                path = runs_dir / f"{base}-{n}"
        run = cls(path)
        public = settings if isinstance(settings, dict) else settings.public_dict()
        _write_json(path / "run.json", {"id": run.id, "created_at": datetime.datetime.now().isoformat(),
                                        "settings": public})
        return run

    def update_info(self, **fields) -> None:
        """Adds or replaces top-level keys of run.json (e.g. where a run came from)."""
        _write_json(self.path / "run.json", {**self.info(), **fields})

    @classmethod
    def open(cls, path) -> "Run":
        return cls(path)

    def info(self) -> dict:
        return json.loads((self.path / "run.json").read_text(encoding="utf-8"))

    def _file(self, stage: str) -> Path:
        return self.path / f"{STAGES.index(stage) + 1:02d}_{stage}.json"

    def save(self, stage: str, data: dict) -> None:
        for later in STAGES[STAGES.index(stage) + 1:]:
            self._file(later).unlink(missing_ok=True)
        _write_json(self._file(stage), data)

    def load(self, stage: str) -> dict | None:
        path = self._file(stage)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def completed(self) -> list[str]:
        return [s for s in STAGES if self._file(s).exists()]

    def add_history(self, entry: dict) -> None:
        """One line per execution of the reviewed tests (by hand or on a schedule); kept when stages are redone."""
        with (self.path / "history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=_json_default, ensure_ascii=False) + "\n")

    def history(self, limit: int = 50) -> list[dict]:
        """The latest executions, newest first."""
        path = self.path / "history.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [json.loads(line) for line in reversed(lines[-limit:]) if line.strip()]

    def last_with_verdicts(self) -> dict | None:
        """The newest execution that ran its tests, however far back the history goes."""
        path = self.path / "history.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for line in reversed(lines):
            if '"verdicts"' in line:
                entry = json.loads(line)
                if entry.get("verdicts"):
                    return entry
        return None

    def _lock_age(self, name: str = "execution.lock") -> float | None:
        """Seconds since the lock `name` was taken; None when there is none."""
        path = self.path / name
        try:
            text = path.read_text(encoding="utf-8")
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            return (now - datetime.datetime.fromisoformat(json.loads(text)["at"])).total_seconds()
        except (ValueError, KeyError, TypeError):  # being written, or damaged: its file time says when
            return now.timestamp() - mtime

    def locked(self, name: str = "execution.lock") -> bool:
        age = self._lock_age(name)
        return age is not None and age < LOCK_STALE_SECONDS

    def execution_lock(self):
        """Held while the run's tests execute, so two executions of one run never overlap, whichever process
        starts them. The file is created exclusively; one older than LOCK_STALE_SECONDS is taken over."""
        return self._exclusive("execution.lock", "another execution of run {id} is in progress")

    def proposal_lock(self):
        """Held while tests are proposed for a structural change found after an execution, so the in-app
        schedule and a `dqvouch rerun` never propose for the same change at once."""
        return self._exclusive("proposal.lock", "tests for a change in run {id} are being proposed")

    @contextlib.contextmanager
    def _exclusive(self, name: str, busy: str):
        path = self.path / name
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if self.locked(name):
                    raise RunBusyError(f"{busy.format(id=self.id)}. If nothing is (a process that was killed), "
                                       f"delete {path}; it is ignored a day after it was taken.")
                path.unlink(missing_ok=True)
        else:
            raise RunBusyError(f"could not take the lock {path}")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}, f)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    def latest(self, key: str, default=_MISSING):
        """The value of `key` in the most recent finished stage that has it."""
        for stage in reversed(self.completed()):
            data = self.load(stage)
            if key in data:
                return data[key]
        if default is _MISSING:
            raise KeyError(f"no finished stage of run {self.id} has {key!r}")
        return default
