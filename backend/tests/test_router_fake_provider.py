# The router and client against a fake Groq server: the real openai client over HTTP (httpx
# MockTransport), Groq-style rate-limit headers, and simulated 429s and 500s. No real API calls and
# nothing written to Mongo. Needs LOCAL=false in .env (the router is built from models.yaml).
# Watch the router's log lines with:
#   ../.venv/bin/python -m pytest tests/test_router_fake_provider.py -o log_cli=true --log-cli-level=INFO
import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from app import db
from app.llm import client as llm
from app.llm import limits, providers, router

pytestmark = pytest.mark.skipif(providers.config is None, reason="LOCAL=true: no cloud models to route")

BIG, SMALL = "openai/gpt-oss-120b", "openai/gpt-oss-20b"


class BreaksAfterFirstWords(httpx.AsyncByteStream):
    """A streamed reply whose connection drops after its first chunk of text."""

    def __init__(self, first: bytes):
        self.first = first

    async def __aiter__(self):
        yield self.first
        raise httpx.ReadError("connection lost")


class FakeGroq:
    """Answers like Groq: x-ratelimit headers, streamed usage in x_groq, 429s for `limited` models.
    `script[model]` is a list of outcomes for that model's next requests: an HTTP status (429, 400,
    500...) or "cut" (a stream that breaks after its first words); when empty, requests succeed."""

    def __init__(self):
        self.limited: set[str] = set()
        self.status = 200
        self.script: dict[str, list] = {}
        self.retry_after = "30"
        self.requests: list[str] = []
        self.remaining_tokens = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/audio/transcriptions"):  # multipart: the model is a form field
            model = request.content.split(b'name="model"')[1].split(b"\r\n\r\n")[1].split(b"\r\n")[0].decode()
            self.requests.append(model)
            if model in self.limited:
                return httpx.Response(429, headers={"retry-after": self.retry_after}, json={"error": {"message": "rate limited"}})
            return httpx.Response(200, json={"text": " remind me to call mom ", "duration": 2.4})
        body = json.loads(request.content)
        model = body["model"]
        self.requests.append(model)
        outcome = self.script.get(model, []).pop(0) if self.script.get(model) else self.status
        if outcome == "cut":
            chunk = {"id": "1", "object": "chat.completion.chunk", "created": 1, "model": model,
                     "choices": [{"index": 0, "delta": {"content": "The answer is"}}]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=BreaksAfterFirstWords(f"data: {json.dumps(chunk)}\n\n".encode()))
        if outcome == 429:
            model = "limited:" + model
        elif outcome != 200:
            return httpx.Response(outcome, json={"error": {"message": f"error {outcome}"}})
        if model.startswith("limited:") or model in self.limited:
            model = model.removeprefix("limited:")
            return httpx.Response(429, headers={"retry-after": self.retry_after}, json={"error": {
                "message": f"Rate limit reached for {model}.", "code": "rate_limit_exceeded"}})
        self.remaining_tokens[model] = self.remaining_tokens.get(model, 8000) - 900
        headers = {"x-ratelimit-remaining-tokens": str(self.remaining_tokens[model]), "x-ratelimit-remaining-requests": "990"}
        usage = {"prompt_tokens": 850, "completion_tokens": 50, "total_tokens": 900}
        if body.get("stream"):
            chunk = {"id": "1", "object": "chat.completion.chunk", "created": 1, "model": model}
            sse = (f"data: {json.dumps({**chunk, 'choices': [{'index': 0, 'delta': {'content': 'hi'}}]})}\n\n"
                   f"data: {json.dumps({**chunk, 'choices': [], 'x_groq': {'usage': usage}})}\n\n"
                   "data: [DONE]\n\n")
            return httpx.Response(200, headers={**headers, "content-type": "text/event-stream"}, content=sse)
        return httpx.Response(200, headers=headers, json={
            "id": "1", "object": "chat.completion", "created": 1, "model": model, "usage": usage,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok": true}'}}]})


@pytest.fixture
def fake(monkeypatch):
    server = FakeGroq()
    groq = providers.config.providers["groq"]
    monkeypatch.setattr(groq, "client", AsyncOpenAI(api_key="x", base_url="http://fake/v1", max_retries=0,
                                                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(server))))
    if "gemini" in providers.config.providers:  # the same fake answers for Gemini's models
        monkeypatch.setattr(providers.config.providers["gemini"], "client", groq.client)
    fresh = limits.Limiter(router.limiter.limits, router.limiter.providers, router.limiter.header_scopes)
    monkeypatch.setattr(router, "limiter", fresh)  # clean budgets, and no Mongo writes (no on_record)
    monkeypatch.setattr(db, "add_llm_usage", lambda *a: asyncio.sleep(0))
    return server


def chat() -> str:
    async def run():
        return "".join(p for p in [p async for p in llm.chat_stream([{"role": "user", "content": "hi"}])]
                       if isinstance(p, str))
    return asyncio.run(run())


def test_budgets_decrease_and_sync_from_headers(fake):
    chat()
    chat()
    left = router.limiter.remaining("gpt-oss-120b")
    assert left["minute_tokens"] == 8000 - 1800 and left["minute_requests"] == 28
    # The provider's header (always "990 left" here) pulled our daily count down; our own next
    # request counted on from there, and a header never gives budget back
    assert left["day_requests"] == 989


def test_429_cools_the_model_down_and_the_next_model_serves(fake):
    fake.limited.add(BIG)
    assert chat() == "hi"
    assert fake.requests == [BIG, SMALL]
    ok, reason, wait = router.limiter.check("gpt-oss-120b", 100)
    assert not ok and reason == "rate-limited (429)" and wait == pytest.approx(30, abs=1)
    fake.limited.clear()
    chat()
    assert fake.requests[-1] == SMALL  # 120b is skipped while cooling down, without a request


def test_all_models_busy_says_when_to_try_again(fake):
    fake.limited |= {BIG, SMALL}
    with pytest.raises(router.AllModelsBusy) as busy:
        chat()
    assert busy.value.user_message() == "All AI models are busy right now. Try again in 30 seconds."


def test_repeated_server_errors_sideline_the_provider(fake):
    fake.status = 500
    with pytest.raises(router.AllModelsBusy):
        chat()  # 120b fails, 20b fails, 120b's second chance fails: the third error sidelines Groq
    assert len(fake.requests) == 3
    with pytest.raises(router.AllModelsBusy) as busy:
        chat()
    assert len(fake.requests) == 3  # nothing sent while sidelined
    assert "sidelined" in str(busy.value)


# --- Stage 3: failover ---

def test_a_server_error_before_any_text_fails_over_to_the_next_model(fake):
    fake.script[BIG] = [500]
    assert chat() == "hi"
    assert fake.requests == [BIG, SMALL]


def test_a_request_error_skips_the_model_without_counting_against_the_provider(fake):
    fake.script[BIG] = [400, 400, 400]
    for _ in range(3):
        assert chat() == "hi"
    assert router.limiter.provider_state["groq"].errors == 0  # a 400 isn't the provider's fault
    assert router.limiter.check("gpt-oss-120b", 100)[0]


def test_a_failure_mid_reply_ends_it_instead_of_switching_models(fake):
    fake.script[BIG] = ["cut"]
    pieces = []

    async def run():
        async for piece in llm.chat_stream([{"role": "user", "content": "hi"}]):
            pieces.append(piece)
    with pytest.raises(llm.ReplyInterrupted):
        asyncio.run(run())
    assert pieces == ["The answer is"]  # the user saw the start, then the error
    assert fake.requests == [BIG]  # no second model was asked


def test_a_single_model_task_gets_a_second_chance_after_a_blip(fake):
    fake.script[SMALL] = [500]
    result = asyncio.run(llm.complete_json([{"role": "user", "content": "x"}], task="background_json"))
    assert result == {"ok": True} and fake.requests == [SMALL, SMALL]


def test_background_work_waits_for_a_short_cooldown(fake):
    fake.retry_after = "1"
    fake.script[SMALL] = [429]
    result = asyncio.run(llm.complete_json([{"role": "user", "content": "x"}], task="background_json"))
    assert result == {"ok": True} and fake.requests == [SMALL, SMALL]


def test_forcing_the_top_chat_model_into_cooldown_moves_chat_but_not_background(fake):
    router.limiter.rate_limited("gpt-oss-120b", {"retry-after": "120"})
    assert chat() == "hi"
    asyncio.run(llm.complete_json([{"role": "user", "content": "x"}], task="background_json"))
    assert fake.requests == [SMALL, SMALL]  # chat moved to 20b; background was already on its own list


def test_background_json_uses_its_own_model(fake):
    result = asyncio.run(llm.complete_json([{"role": "user", "content": "x"}], task="background_json"))
    assert result == {"ok": True} and fake.requests == [SMALL]


# --- personal data ---

GEMINI = "gemini-3.5-flash-lite"
needs_gemini = pytest.mark.skipif("gemini" not in providers.config.providers if providers.config else True,
                                  reason="no gemini provider in models.yaml")


@needs_gemini
def test_titles_fail_over_to_gemini(fake):
    fake.limited.add(SMALL)
    result = asyncio.run(llm.complete_json([{"role": "user", "content": "x"}], task="titles"))
    assert result == {"ok": True} and fake.requests == [SMALL, GEMINI]


@needs_gemini
def test_personal_data_never_reaches_a_provider_not_approved_for_it(fake):
    assert not providers.config.providers["gemini"].allow_personal_data
    fake.retry_after = "300"  # longer than background work waits, so it gives up at once
    fake.limited |= {BIG, SMALL}
    with pytest.raises(router.AllModelsBusy):
        chat()
    with pytest.raises(router.AllModelsBusy):
        asyncio.run(llm.complete_json([{"role": "user", "content": "x"}], task="background_json"))
    assert not [m for m in fake.requests if m.startswith("gemini")]


@needs_gemini
def test_the_eval_can_check_a_model_that_memory_extraction_cannot_use(fake, monkeypatch):
    from app.llm.providers import Task
    from app.memory import writer as memory_writer
    gemini = providers.config.models[GEMINI]
    monkeypatch.setitem(providers.config.tasks, "eval", Task("eval", False, ["json_mode"], [gemini], 400))
    monkeypatch.setattr(memory_writer, "EXTRACT_TASK", "eval")
    asyncio.run(memory_writer.extract_facts("I live in Atlanta", "Nice!"))
    assert fake.requests == [GEMINI]
    monkeypatch.setattr(memory_writer, "EXTRACT_TASK", "background_json")
    asyncio.run(memory_writer.extract_facts("I live in Atlanta", "Nice!"))
    assert fake.requests == [GEMINI, SMALL]  # real extraction stays on approved providers


# --- transcription (voice mode) ---

def test_transcription_goes_through_the_router(fake):
    text = asyncio.run(llm.transcribe(b"fake audio", "speech.m4a", "en", seconds=3.0))
    assert text == "remind me to call mom" and fake.requests == ["whisper-large-v3-turbo"]
    left = router.limiter.remaining("whisper-large-v3-turbo")
    assert left["minute_requests"] == 19
    assert left["hour_audio"] == pytest.approx(7200 - 2.4)  # Whisper's measured 2.4s, not the browser's 3.0s


def test_transcription_skips_a_model_out_of_audio_seconds(fake):
    turbo = router.limiter.models["whisper-large-v3-turbo"]
    turbo.hour_audio.append([router.limiter.clock.now(), 7199.0])
    asyncio.run(llm.transcribe(b"fake audio", "speech.m4a", seconds=5.0))
    assert fake.requests == ["whisper-large-v3"]  # turbo couldn't fit 5 more seconds this hour


def test_transcription_fails_over_when_turbo_is_rate_limited(fake):
    fake.limited.add("whisper-large-v3-turbo")
    assert asyncio.run(llm.transcribe(b"fake audio", "speech.m4a")) == "remind me to call mom"
    assert fake.requests == ["whisper-large-v3-turbo", "whisper-large-v3"]
