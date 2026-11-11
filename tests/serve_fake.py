"""The web interface on the fictional orders example, with the scripted FakeLLM instead of Gemini: for developing and
checking the UI without an API key or quota. Runs and the .env go to a temporary folder.

    python tests/serve_fake.py [CONFIG] [--slow SECONDS]

CONFIG defaults to examples/orders/dqvouch.toml (PostgreSQL); --slow makes every model call take that long, to watch
the progress messages or reload the page while a stage runs. Never imported by the package.
"""
import argparse
import pathlib
import sys
import tempfile
import time

import uvicorn

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fake_llm import FakeLLM  # noqa: E402

from dqvouch.server import create_app  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("config", nargs="?", default=str(HERE.parent / "examples" / "orders" / "dqvouch.toml"))
parser.add_argument("--slow", type=float, default=0.0)
parser.add_argument("--port", type=int, default=8765)
args = parser.parse_args()


def make_llm(settings, key):
    fake = FakeLLM()

    def llm(prompt):
        time.sleep(args.slow)
        return fake(prompt)
    return llm


work = pathlib.Path(tempfile.mkdtemp(prefix="dqvouch-ui-"))
app = create_app(work / "runs", work / ".env", args.config, llm_factory=make_llm)
print(f"FakeLLM interface on http://127.0.0.1:{args.port} (config {args.config}, runs in {work})")
uvicorn.run(app, host="127.0.0.1", port=args.port)
