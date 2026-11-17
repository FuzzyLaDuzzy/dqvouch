import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from dqvouch import llm as llm_module
from dqvouch.llm import OpenAICompatibleLLM, list_models, list_openai_models, make_llm


def test_list_models_keeps_generate_content_models(monkeypatch):
    models = [SimpleNamespace(name="models/gemini-3.5-flash-lite", supported_actions=["generateContent"]),
              SimpleNamespace(name="models/text-embedding-9", supported_actions=["embedContent"]),
              SimpleNamespace(name="models/gemini-x", supported_actions=["generateContent", "countTokens"])]

    class FakeClient:
        def __init__(self, api_key):
            assert api_key == "k"
            self.models = SimpleNamespace(list=lambda: iter(models))

    monkeypatch.setattr(llm_module.genai, "Client", FakeClient)
    assert list_models("k") == ["gemini-3.5-flash-lite", "gemini-x"]


def test_retries_are_reported_and_cleared(monkeypatch):
    answers = [RuntimeError("503 UNAVAILABLE"), RuntimeError("503 UNAVAILABLE"), SimpleNamespace(text="ok")]

    def generate_content(**kwargs):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    class FakeClient:
        def __init__(self, api_key):
            self.models = SimpleNamespace(generate_content=generate_content)

    monkeypatch.setattr(llm_module.genai, "Client", FakeClient)
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)
    notes = []
    llm = llm_module.GeminiLLM("m", "k")
    llm.on_retry = notes.append
    assert llm("p") == "ok"
    assert [n.startswith("Gemini is busy") for n in notes[:2]] == [True, True]
    assert "attempt 3" in notes[1]
    assert notes[-1] is None  # answered: the note goes away


# --- any OpenAI-compatible server (Ollama, LM Studio, vLLM, OpenAI): a fake one on a free local port ---

@pytest.fixture
def openai_server():
    state = {"requests": [], "answers": []}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            state["requests"].append({"path": self.path, "headers": dict(self.headers), "body": body})
            status, answer = state["answers"].pop(0) if state["answers"] else (200, {"data": [{"id": "b"}, {"id": "a"}]})
            data = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _reply

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield state
    server.shutdown()


def chat(text):
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}}]}


def test_openai_compatible_sends_the_prompt_at_temperature_0(openai_server):
    openai_server["answers"].append(chat("```json\n{}\n```"))
    assert OpenAICompatibleLLM("llama3", openai_server["url"])("the prompt") == "```json\n{}\n```"
    request = openai_server["requests"][0]
    assert request["path"] == "/v1/chat/completions"
    assert request["body"]["model"] == "llama3" and request["body"]["temperature"] == 0
    assert request["body"]["messages"] == [{"role": "user", "content": "the prompt"}]


def test_openai_compatible_retries_busy_answers_and_reports_them(openai_server, monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)
    openai_server["answers"] += [(429, {"error": {"message": "slow down"}}), (503, {}), chat("ok")]
    notes = []
    llm = OpenAICompatibleLLM("m", openai_server["url"])
    llm.on_retry = notes.append
    assert llm("p") == "ok"
    assert "127.0.0.1" in notes[0] and "busy" in notes[0] and "attempt 3" in notes[1]
    assert notes[-1] is None and len(openai_server["requests"]) == 3


def test_openai_compatible_does_not_retry_a_bad_request(openai_server):
    openai_server["answers"].append((400, {"error": {"message": "bad model"}}))
    with pytest.raises(RuntimeError, match="bad model"):
        OpenAICompatibleLLM("m", openai_server["url"])("p")
    assert len(openai_server["requests"]) == 1


def test_the_key_is_sent_only_when_set(openai_server):
    openai_server["answers"] += [chat("a"), chat("b")]
    OpenAICompatibleLLM("m", openai_server["url"])("p")
    OpenAICompatibleLLM("m", openai_server["url"], api_key="k")("p")
    first, second = (r["headers"] for r in openai_server["requests"])
    assert "Authorization" not in first and second["Authorization"] == "Bearer k"


def test_base_url_with_a_trailing_slash_works(openai_server):
    openai_server["answers"].append(chat("ok"))
    assert OpenAICompatibleLLM("m", openai_server["url"] + "/")("p") == "ok"
    assert openai_server["requests"][0]["path"] == "/v1/chat/completions"


def test_a_server_that_is_not_there_fails_after_a_few_tries(monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)
    notes = []
    llm = OpenAICompatibleLLM("m", "http://127.0.0.1:9/v1")  # nothing listens on port 9
    llm.on_retry = notes.append
    with pytest.raises(RuntimeError, match="cannot reach"):
        llm("p")
    assert 1 <= len(notes) <= 4


def test_openai_models_are_listed(openai_server):
    assert list_openai_models(openai_server["url"]) == ["a", "b"]
    assert openai_server["requests"][0]["path"] == "/v1/models"


def test_make_llm_follows_the_runs_provider():
    """A run made before providers existed has none in its settings: it is a Gemini run."""
    assert isinstance(make_llm({"model": "gemini-x"}, "k"), llm_module.GeminiLLM)
    local = make_llm({"model": "llama3", "provider": "openai", "base_url": "http://localhost:11434/v1"}, None)
    assert isinstance(local, OpenAICompatibleLLM) and local.model_id == "llama3"


def test_a_limit_on_attempts_turns_a_long_gemini_outage_into_an_error(monkeypatch):
    calls = []

    def generate_content(**kwargs):
        calls.append(1)
        raise RuntimeError("503 UNAVAILABLE")

    class FakeClient:
        def __init__(self, api_key):
            self.models = SimpleNamespace(generate_content=generate_content)

    monkeypatch.setattr(llm_module.genai, "Client", FakeClient)
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)
    llm = llm_module.GeminiLLM("m", "k")
    llm.max_attempts = 3  # what a scheduled execution sets: an outage must end, not hang the schedule
    with pytest.raises(RuntimeError, match="503"):
        llm("p")
    assert len(calls) == 3


def test_a_limit_on_attempts_turns_a_long_busy_server_into_an_error(openai_server, monkeypatch):
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)
    openai_server["answers"] += [(503, {})] * 5
    llm = OpenAICompatibleLLM("m", openai_server["url"])
    llm.max_attempts = 2
    with pytest.raises(Exception, match="503"):
        llm("p")
    assert len(openai_server["requests"]) == 2
