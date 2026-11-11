"""The model clients -- Gemini, or any server that speaks OpenAI's chat completions API (Ollama, LM Studio, vLLM,
OpenAI...) -- at temperature 0, retrying transient errors; and the extraction of the JSON block from an answer."""
import json
import random
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from google import genai


# Markers that indicate a TRANSIENT failure worth retrying (server-side
# capacity/rate issues), as opposed to a genuine request problem (bad
# prompt, auth failure, invalid model name) that retrying would never fix.
# Matched against the exception's string representation rather than a
# specific SDK exception class, since that's stable across google-genai
# versions and is exactly what a 503 UNAVAILABLE / 429 RESOURCE_EXHAUSTED /
# 5xx error looks like when printed.
_TRANSIENT_GEMINI_ERROR_MARKERS = (
    "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
    "500", "INTERNAL", "502", "504", "DEADLINE_EXCEEDED", "timeout",
)


def _is_transient_gemini_error(exc: Exception) -> bool:
    text = str(exc)
    return any(marker in text for marker in _TRANSIENT_GEMINI_ERROR_MARKERS)


# A 429 RESOURCE_EXHAUSTED can mean two very different things: a short,
# genuinely transient per-minute rate limit (worth retrying -- it clears up
# in seconds), or a PER-DAY quota that is used up. For a per-day cap every
# retry for the rest of the day gets the same 429 back, so it must stop at
# once. Matched on "PerDay", which appears in the quotaId of every daily-cap
# violation the API returns.
_DAILY_QUOTA_MARKERS = ("PerDay", "per_day", "RequestsPerDay")


def _is_daily_quota_exhausted(exc: Exception) -> bool:
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text and any(marker in text for marker in _DAILY_QUOTA_MARKERS)


class QuotaExhaustedError(RuntimeError):
    """The model's daily request quota is used up; retrying will not help until it resets."""


class GeminiLLM:
    """Callable LLM client: `llm(prompt) -> str`. Every stage takes one of these (or any function with the same
    shape), so tests can pass a fake and the model is chosen in one place."""

    def __init__(self, model_id: str, api_key: str | None = None,
                 base_delay_seconds: float = 0.5, max_delay_seconds: float = 5.0):
        self.model_id = model_id
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self._client = genai.Client(api_key=api_key)
        self.on_retry = None  # on_retry(note) while it waits to try again, on_retry(None) once answered

    def __call__(self, prompt: str) -> str:
        """
        Retries INDEFINITELY on transient errors (503 UNAVAILABLE, 429 rate
        limits, 5xx): a run can do a lot of real work (metadata, profiling
        SQL) before reaching an LLM call, and a momentary capacity spike on
        the provider's side should not throw that away. Backoff doubles each
        attempt but is capped at max_delay_seconds. Does NOT retry errors that
        do not look transient (bad prompt, auth failure, invalid model name)
        nor an exhausted daily quota -- those fail immediately.
        """
        print(f"Sending data to {self.model_id}...")
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.models.generate_content(
                    model=self.model_id,
                    contents=prompt,
                    # temperature=0 so repeated runs of the same context are comparable
                    config=genai.types.GenerateContentConfig(temperature=0),
                )
                if attempt > 1 and self.on_retry:
                    self.on_retry(None)
                return response.text
            except Exception as e:
                if _is_daily_quota_exhausted(e):
                    raise QuotaExhaustedError(
                        f"Gemini daily quota exhausted for model {self.model_id}. Retrying will not help until the "
                        f"quota resets; choose a model with a higher quota or try again later. Original error: {e}"
                    ) from e
                if not _is_transient_gemini_error(e):
                    raise
                delay = (min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
                         + random.uniform(0, 1.5))
                print(f"Transient error from {self.model_id} (attempt {attempt}): {e}")
                print(f"Retrying in {delay:.1f}s...")
                if self.on_retry:
                    code = getattr(e, "code", None)
                    self.on_retry(f"Gemini is busy{f' ({code})' if code else ''}; trying again in {delay:.0f} s "
                                  f"(attempt {attempt + 1})")
                time.sleep(delay)


_TRANSIENT_HTTP = {429, 500, 502, 503, 504}
_UNREACHABLE_TRIES = 3  # connection refused or unknown host: the server is not running, or the address is wrong


def _request(url: str, api_key: str | None, body: dict | None = None, timeout: float = 60) -> dict:
    """One JSON request (POST when there is a body) with the key as a bearer token when there is one."""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _error_text(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        return str(e)
    try:
        error = json.loads(body).get("error")
        return str(error.get("message") if isinstance(error, dict) else error or body)
    except (ValueError, AttributeError):
        return body[:300]


class OpenAICompatibleLLM:
    """Same shape as GeminiLLM (`llm(prompt) -> str`, `on_retry`) for any server with OpenAI's chat completions API,
    a local one (Ollama, LM Studio, vLLM, llama.cpp) or a hosted one. Busy answers (429, 5xx) and an unreachable
    server are retried indefinitely, like Gemini's transient errors; any other refusal fails at once."""

    def __init__(self, model_id: str, base_url: str, api_key: str | None = None, timeout: float = 600,
                 base_delay_seconds: float = 0.5, max_delay_seconds: float = 5.0):
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout  # local models can take minutes on a large prompt
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.on_retry = None  # on_retry(note) while it waits to try again, on_retry(None) once answered
        self._host = urlsplit(self.base_url).netloc or self.base_url

    def __call__(self, prompt: str) -> str:
        print(f"Sending data to {self.model_id} at {self._host}...")
        body = {"model": self.model_id, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
        attempt = unreachable = 0
        while True:
            attempt += 1
            try:
                answer = _request(f"{self.base_url}/chat/completions", self.api_key, body, self.timeout)
            except urllib.error.HTTPError as e:
                if e.code not in _TRANSIENT_HTTP:
                    raise RuntimeError(f"{self._host} refused the request ({e.code}): {_error_text(e)}") from e
                why = f"{self._host} is busy ({e.code})"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                reason = getattr(e, "reason", e)
                if isinstance(e, urllib.error.URLError) and not isinstance(reason, TimeoutError):
                    unreachable += 1
                    if unreachable >= _UNREACHABLE_TRIES:
                        raise RuntimeError(f"cannot reach the model server at {self.base_url} ({reason}): is it "
                                           f"running, and is the address right?") from e
                why = f"{self._host} is not answering ({reason})"
            else:
                if attempt > 1 and self.on_retry:
                    self.on_retry(None)
                try:
                    return answer["choices"][0]["message"]["content"] or ""
                except (KeyError, IndexError, TypeError) as e:
                    raise RuntimeError(f"unexpected answer from {self._host}: {str(answer)[:300]}") from e
            delay = (min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
                     + random.uniform(0, 1.5))
            print(f"{why}; retrying in {delay:.1f}s...")
            if self.on_retry:
                self.on_retry(f"{why}; trying again in {delay:.0f} s (attempt {attempt + 1})")
            time.sleep(delay)


def list_openai_models(base_url: str, api_key: str | None = None) -> list[str]:
    """The models an OpenAI-compatible server offers (GET /models)."""
    return sorted(m["id"] for m in _request(f"{base_url.rstrip('/')}/models", api_key, timeout=15)["data"])


def make_llm(settings: dict, key: str | None):
    """The client for a run's settings (`provider`, `model`, `base_url`); a run without a provider is a Gemini run."""
    if settings.get("provider", "gemini") == "openai":
        return OpenAICompatibleLLM(settings["model"], settings["base_url"], key)
    return GeminiLLM(settings["model"], key)


def extract_dq_json(llm_response_text: str) -> dict:
    """
    Pulls the ```json ... ``` fenced block out of the LLM's response and
    parses it -- this is how Gemini is prompted to respond (see the "Output
    Format" sections of the two prompt builders).

    Falls back to parsing the ENTIRE response as raw JSON when no fenced
    block is found (some models answer with bare JSON). Only raises
    ValueError (with a clear message) when NEITHER form parses.
    """
    match = re.search(r"```json\s*(.*?)\s*```", llm_response_text, re.DOTALL)
    if match:
        json_text = match.group(1)
        try:
            return json.loads(json_text)
        except json.JSONDecodeError as e:
            raise ValueError(f"The JSON block was found but did not parse: {e}") from e

    try:
        return json.loads(llm_response_text.strip())
    except json.JSONDecodeError as e:
        raise ValueError(
            "Could not find a ```json ... ``` code block in the LLM response, and the raw "
            f"response did not parse as JSON either ({e}). Nothing was sent to the SQL generator."
        ) from e


def list_models(api_key: str) -> list[str]:
    """The Gemini models this key can use to generate text, by name without the "models/" prefix. A bad key raises
    whatever the client raises."""
    models = genai.Client(api_key=api_key).models.list()
    return sorted(m.name.removeprefix("models/") for m in models if "generateContent" in (m.supported_actions or []))
