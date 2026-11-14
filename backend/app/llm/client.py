# Streaming LLM client. With LOCAL=true everything goes to Ollama (plain chat and JSON). Otherwise
# each request names a task, the router (router.py, models.yaml) picks its provider and model, and
# every provider is reached through the same OpenAI-compatible client. Each request reserves its
# estimated tokens with the router's rate limiter first and reports its actual usage and the
# provider's rate-limit headers after. A 429 or an error moves the request to the task's next
# model with budget (router.Failover), but only until text has reached the user: a failure
# mid-reply ends it with ReplyInterrupted instead. Every request is logged with its provider and model.
import asyncio
import json

import httpx
from openai import APIError, RateLimitError  # noqa: F401 (RateLimitError is re-exported)

from app.core.config import CHAT_MODEL, LOCAL, MAX_TOOL_ROUNDS, OLLAMA_URL
from app.core.logs import get_logger
from app.llm import router

log = get_logger("llm")

TIMEOUT = httpx.Timeout(120.0, connect=5.0)
CHAT_TASK = "chat_tools"


def _chunk_usage(chunk):
    """A streamed chunk's token usage: the standard field, or Groq's x_groq.usage on the last chunk."""
    if chunk.usage:
        return chunk.usage
    extra = getattr(chunk, "x_groq", None)
    return extra.get("usage") if isinstance(extra, dict) else getattr(extra, "usage", None)


async def chat_stream(messages: list[dict], tools=None, ctx=None):
    """Yields reply text (str). With a tool registry (cloud models only), runs the tool loop and also
    yields dict events: {"type": "tool_start", ...}, {"type": "tool_end", ...}, and
    {"type": "retract", "chars": n}, meaning the last n characters of text should be dropped."""
    stream = _ollama_stream(messages) if LOCAL else _tool_loop(messages, tools, ctx)

    async for piece in stream:
        yield piece


async def _ollama_stream(messages: list[dict]):
    payload = {"model": CHAT_MODEL, "messages": messages, "stream": True}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode()
                raise RuntimeError(f"Ollama {resp.status_code}: {body}")

            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise RuntimeError(chunk["error"])
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    yield piece
                if chunk.get("done"):
                    break

async def _tool_loop(messages: list[dict], registry, ctx):
    messages = list(messages)
    requests, tokens = 0, 0
    try:
        async for piece in _tool_rounds(messages, registry, ctx):
            if isinstance(piece, tuple):  # ("usage", tokens) bookkeeping from a finished request
                requests, tokens = requests + 1, tokens + piece[1]
                continue
            yield piece
    finally:
        if requests:
            log.info("message total: %d tokens over %d request(s)", tokens, requests)


class ReplyInterrupted(Exception):
    """A provider failed after part of the reply had already reached the user. Switching models
    then would restart the answer mid-sentence, so the reply ends with an error instead."""


def _is_tool_use_failed(error) -> bool:
    return getattr(error, "code", None) == "tool_use_failed" or "tool_use_failed" in str(error)


async def _tool_rounds(messages: list[dict], registry, ctx):
    rounds = 0
    while True:
        # Out of rounds: one last call with tools disabled so the model has to answer
        final = rounds >= MAX_TOOL_ROUNDS
        schemas = registry.schemas(ctx) if registry else []
        tool_args = {"tools": schemas, "tool_choice": "none" if final else "auto"} if schemas else {}
        # Each round is its own request: the first model in the task's order with budget serves
        # it, and a model that fails before any text reached the user hands over to the next
        pick = router.Failover(CHAT_TASK, router.estimate(CHAT_TASK, messages, schemas))
        retry_round = False
        while True:
            model = await pick.next()  # raises AllModelsBusy when nothing is left
            reservation = router.reserve(model, pick.tokens)
            text, calls = [], {}
            try:
                raw = await model.provider.client.chat.completions.with_raw_response.create(
                    model=model.id,
                    messages=messages,
                    stream=True,
                    **({"stream_options": {"include_usage": True}} if model.provider.stream_usage else {}),
                    **tool_args,
                    **model.options,
                )
                stream = raw.parse()
                async for chunk in stream:
                    if usage := _chunk_usage(chunk):
                        yield ("usage", router.served(CHAT_TASK, model, "chat", usage, reservation, raw.headers))
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        text.append(delta.content)
                        yield delta.content
                    # Tool calls arrive in fragments keyed by index; stitch them together
                    for tc in delta.tool_calls or []:
                        call = calls.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
                        call["id"] = tc.id or call["id"]
                        if tc.function:
                            call["name"] += tc.function.name or ""
                            call["arguments"] += tc.function.arguments or ""
                router.unfinished(reservation)  # no-op if usage arrived; else count the estimate
                break
            except RateLimitError as e:
                pick.rate_limited(model, e, reservation)
                if text:
                    raise ReplyInterrupted(f"{model.label} was rate-limited mid-reply") from e
            except APIError as e:
                # Groq rejects tool calls the model formatted badly, sometimes mid-stream: the
                # model's fault, not the provider's, so the round is retried, taking back its text
                if _is_tool_use_failed(e) and not final:
                    router.unfinished(reservation)  # the provider did the work; count it
                    log.warning("model produced a malformed tool call, retrying: %s", e)
                    if text:
                        yield {"type": "retract", "chars": len("".join(text))}
                    retry_round = True
                    break
                pick.failed(model, e, reservation)
                if text:
                    raise ReplyInterrupted(f"{model.label} failed mid-reply: {type(e).__name__}") from e
            except httpx.HTTPError as e:  # a network error while streaming, not wrapped by the SDK
                pick.failed(model, e, reservation)
                if text:
                    raise ReplyInterrupted(f"{model.label} failed mid-reply: {type(e).__name__}") from e
        if retry_round:
            rounds += 1
            continue

        if not calls:
            return

        # Text written before a tool call is usually a premature answer that gets repeated once the
        # result is in. Keep it in the model's context below but take it back from the user.
        if text:
            log.debug("retracting %d chars written before a tool call", len("".join(text)))
            yield {"type": "retract", "chars": len("".join(text))}
        rounds += 1
        ordered = [calls[i] for i in sorted(calls)]
        messages.append({
            "role": "assistant",
            "content": "".join(text) or None,
            "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for c in ordered
            ],
        })
        for c in ordered:
            yield {"type": "tool_start", "id": c["id"], "name": c["name"],
                   "label": registry.running_label(c["name"])}
            result = await registry.call(c["name"], c["arguments"], ctx)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            yield {"type": "tool_end", "id": c["id"], "name": c["name"], **registry.describe(c["name"], result)}


async def complete_json(messages: list[dict], task: str, purpose: str = "json") -> dict:
    """One JSON-mode completion for a background task (background_json, titles, morning_brief)."""
    text = await _ollama_complete(messages) if LOCAL else await _cloud_complete(messages, task, purpose)
    return _parse_json(text)


async def _ollama_complete(messages: list[dict]) -> str:
    payload = {
        "model": CHAT_MODEL,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama {resp.status_code}: {resp.text}")
    return resp.json()["message"]["content"]


BACKGROUND_MAX_WAIT = 60.0  # a background call waits this long at most for a model to free up


async def _cloud_complete(messages: list[dict], task: str, purpose: str) -> str:
    """Tries the task's models in order, failing over on 429s and errors (router.Failover). If all
    are busy, waits for the first to free up (up to BACKGROUND_MAX_WAIT, twice), as background
    work isn't in a hurry."""
    estimate = router.estimate(task, messages)
    pick, waits = router.Failover(task, estimate), 0
    while True:
        try:
            model = await pick.next()
        except router.AllModelsBusy as busy:
            if waits == 2 or busy.wait > BACKGROUND_MAX_WAIT:
                router.record_event("busy", task, None, "; ".join(busy.reasons))
                raise
            waits += 1
            log.info("router: %s waits %.0fs for a model to free up", task, busy.wait)
            await asyncio.sleep(busy.wait + 0.5)
            pick = router.Failover(task, estimate)
            continue
        reservation = router.reserve(model, estimate)
        try:
            raw = await model.provider.client.chat.completions.with_raw_response.create(
                model=model.id,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
                **model.options,
            )
        except RateLimitError as e:
            pick.rate_limited(model, e, reservation)
            continue
        except (APIError, httpx.HTTPError) as e:
            pick.failed(model, e, reservation)
            continue
        resp = raw.parse()
        router.served(task, model, purpose, resp.usage, reservation, raw.headers)
        return resp.choices[0].message.content


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)

async def transcribe(audio: bytes, filename: str, language: str | None = None, seconds: float = 0.0) -> str:
    """Speech to text for voice mode, through the router's "transcribe" task (Whisper on Groq):
    failover on 429s and errors like every other request. `seconds` is the recording's length (from
    the browser), checked against the models' audio-seconds-per-hour/day limits; Whisper's own
    measurement replaces it afterwards. Raises AllModelsBusy if no model is free."""
    pick = router.Failover("transcribe", 0, audio=seconds)
    while True:
        model = await pick.next()
        reservation = router.reserve(model, 0, seconds)
        try:
            raw = await model.provider.client.audio.transcriptions.with_raw_response.create(
                model=model.id,
                file=(filename, audio),
                **({"language": language} if language else {}),
                response_format="verbose_json",  # includes the audio's duration
                temperature=0,
            )
        except RateLimitError as e:
            pick.rate_limited(model, e, reservation)
            continue
        except (APIError, httpx.HTTPError) as e:
            pick.failed(model, e, reservation)
            continue
        result = raw.parse()
        duration = getattr(result, "duration", None)
        router.served("transcribe", model, f"{len(audio) // 1024} KB", None, reservation, raw.headers,
                      audio_seconds=float(duration) if duration is not None else seconds)
        return result.text.strip()
