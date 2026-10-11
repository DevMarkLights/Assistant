# Streaming LLM client that routes to Ollama (local) or Groq (cloud) based on the LOCAL config flag.
# The Groq path can also run a tool-calling loop; Ollama is plain chat. Chat falls back to
# GROQ_FALLBACK_MODELS when GROQ_MODEL is rate-limited, and every Groq request logs its token usage.
import json
from groq import APIError, AsyncGroq, RateLimitError

import httpx

from config import (CHAT_MODEL, GROQ_API_KEY, GROQ_FALLBACK_MODELS, GROQ_MODEL, LOCAL, MAX_TOOL_ROUNDS,
                    OLLAMA_URL)
from logs import get_logger

log = get_logger("llm")

TIMEOUT = httpx.Timeout(120.0, connect=5.0)

groq_client = None
if not LOCAL:
    if not GROQ_API_KEY:
        raise RuntimeError("LOCAL=false requires GROQ_API_KEY in .env")
    groq_client = AsyncGroq(api_key=GROQ_API_KEY, timeout=TIMEOUT, max_retries=2)

CHAT_MODELS = [GROQ_MODEL, *GROQ_FALLBACK_MODELS]


def _chunk_usage(chunk):
    """Groq reports token usage on the last streamed chunk, in x_groq.usage."""
    return chunk.usage or (chunk.x_groq.usage if chunk.x_groq else None)


def log_usage(purpose: str, model: str, usage) -> int:
    """Logs one request's token usage; returns its total so callers can add it up."""
    if not usage:
        return 0
    log.info("%s %s: %d in + %d out = %d tokens", purpose, model,
             usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
    return usage.total_tokens


async def chat_stream(messages: list[dict], tools=None, ctx=None):
    """Yields reply text (str). With a tool registry on the Groq path, runs the tool loop and also
    yields dict events: {"type": "tool_start", ...}, {"type": "tool_end", ...}, and
    {"type": "retract", "chars": n}, meaning the last n characters of text should be dropped."""
    if LOCAL:
        stream = _ollama_stream(messages)
    elif tools is not None:
        stream = _groq_tool_loop(messages, tools, ctx)
    else:
        stream = _groq_stream(messages)

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

async def _groq_stream(messages: list[dict]):
    if groq_client is None:
        raise RuntimeError("GROQ_API_KEY is not set in .env")

    for i, model in enumerate(CHAT_MODELS):
        try:
            stream = await groq_client.chat.completions.create(model=model, messages=messages, stream=True)
            break
        except RateLimitError:
            if i + 1 == len(CHAT_MODELS):
                raise
            log.warning("%s is rate-limited, falling back to %s", model, CHAT_MODELS[i + 1])
    async for chunk in stream:
        log_usage("chat", model, _chunk_usage(chunk))
        if not chunk.choices:
            continue
        piece = chunk.choices[0].delta.content
        if piece:
            yield piece


async def _groq_tool_loop(messages: list[dict], registry, ctx):
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


async def _tool_rounds(messages: list[dict], registry, ctx):
    rounds, model_index = 0, 0
    while True:
        model = CHAT_MODELS[model_index]
        # Out of rounds: one last call with tools disabled so the model has to answer
        final = rounds >= MAX_TOOL_ROUNDS
        text, calls = [], {}
        try:
            stream = await groq_client.chat.completions.create(
                model=model,
                messages=messages,
                tools=registry.schemas(ctx),
                tool_choice="none" if final else "auto",
                stream=True,
            )
            async for chunk in stream:
                if usage := _chunk_usage(chunk):
                    yield ("usage", log_usage("chat", model, usage))
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
        except RateLimitError:
            # Each Groq model has its own limits: retry this round on the next model and stay on it
            if model_index + 1 == len(CHAT_MODELS):
                raise
            model_index += 1
            log.warning("%s is rate-limited, falling back to %s", model, CHAT_MODELS[model_index])
            if text:
                yield {"type": "retract", "chars": len("".join(text))}
            continue
        except APIError as e:
            # Groq rejects tool calls the model formatted badly, sometimes mid-stream; retry the
            # round instead of failing, and take back any text the failed attempt streamed
            if "tool_use_failed" in str(e) and not final:
                log.warning("model produced a malformed tool call, retrying: %s", e)
                if text:
                    yield {"type": "retract", "chars": len("".join(text))}
                rounds += 1
                continue
            raise

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


async def complete_json(messages: list[dict], model: str | None = None, purpose: str = "json") -> dict:
    if LOCAL:
        text = await _ollama_complete(messages)
    else:
        text = await _groq_complete(messages, model or GROQ_MODEL, purpose)
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


async def _groq_complete(messages: list[dict], model: str, purpose: str) -> str:
    resp = await groq_client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        response_format={"type": "json_object"},
    )
    log_usage(purpose, model, resp.usage)
    return resp.choices[0].message.content


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)