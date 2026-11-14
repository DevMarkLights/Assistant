# Streaming LLM client. With LOCAL=true everything goes to Ollama (plain chat and JSON). Otherwise
# each request names a task, the router (router.py, models.yaml) picks its provider and model, and
# every provider is reached through the same OpenAI-compatible client. Chat falls back to the task's
# next model when one is rate-limited, and every request is logged with its provider, model and tokens.
import asyncio
import json

import httpx
from openai import APIConnectionError, APIError, InternalServerError, RateLimitError  # noqa: F401 (re-exported)

from app.core.config import CHAT_MODEL, LOCAL, MAX_TOOL_ROUNDS, OLLAMA_URL
from app.core.logs import get_logger
from app.llm import router

log = get_logger("llm")

TIMEOUT = httpx.Timeout(120.0, connect=5.0)
TRANSIENT = (APIConnectionError, InternalServerError)
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


async def _tool_rounds(messages: list[dict], registry, ctx):
    models = router.candidates(CHAT_TASK)
    rounds, model_index, retried = 0, 0, False
    while True:
        model = models[model_index]
        # Out of rounds: one last call with tools disabled so the model has to answer
        final = rounds >= MAX_TOOL_ROUNDS
        text, calls = [], {}
        schemas = registry.schemas(ctx) if registry else []
        tool_args = {"tools": schemas, "tool_choice": "none" if final else "auto"} if schemas else {}
        try:
            stream = await model.provider.client.chat.completions.create(
                model=model.id,
                messages=messages,
                stream=True,
                **tool_args,
                **model.options,
            )
            async for chunk in stream:
                if usage := _chunk_usage(chunk):
                    yield ("usage", router.served(CHAT_TASK, model, "chat", usage))
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
            # Each model has its own limits: retry this round on the task's next model and stay on it
            if model_index + 1 == len(models):
                raise
            model_index += 1
            log.warning("%s is rate-limited, falling back to %s", model.label, models[model_index].label)
            if text:
                yield {"type": "retract", "chars": len("".join(text))}
            continue
        except TRANSIENT as e:
            # A brief server or network problem: retry the round once, then give up
            if retried:
                raise
            retried = True
            log.warning("%s request failed (%s), retrying once", model.label, type(e).__name__)
            if text:
                yield {"type": "retract", "chars": len("".join(text))}
            await asyncio.sleep(1)
            continue
        except APIError as e:
            # Groq rejects tool calls the model formatted badly, sometimes mid-stream; retry the
            # round instead of failing, and take back any text the failed attempt streamed
            if (getattr(e, "code", None) == "tool_use_failed" or "tool_use_failed" in str(e)) and not final:
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


async def _cloud_complete(messages: list[dict], task: str, purpose: str) -> str:
    models = router.candidates(task)
    for i, model in enumerate(models):
        try:
            resp = await model.provider.background_client.chat.completions.create(
                model=model.id,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
                **model.options,
            )
        except RateLimitError:
            if i + 1 == len(models):
                raise
            log.warning("%s is rate-limited, falling back to %s", model.label, models[i + 1].label)
            continue
        router.served(task, model, purpose, resp.usage)
        return resp.choices[0].message.content


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)