# Streaming LLM client that routes to Ollama (local) or Groq (cloud) based on the LOCAL config flag.
import json
from groq import AsyncGroq

import httpx

from config import OLLAMA_URL, CHAT_MODEL, GROQ_API_KEY, GROQ_MODEL, GROQ_URL, LOCAL

TIMEOUT = httpx.Timeout(120.0, connect=5.0)

groq_client = None
if not LOCAL:
    if not GROQ_API_KEY:
        raise RuntimeError("LOCAL=false requires GROQ_API_KEY in .env")
    groq_client = AsyncGroq(api_key=GROQ_API_KEY, timeout=TIMEOUT, max_retries=2)


async def chat_stream(messages: list[dict]):
    if LOCAL:
        stream = _ollama_stream(messages)
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

    stream = await groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        piece = chunk.choices[0].delta.content
        if piece:
            yield piece


async def complete_json(messages: list[dict]) -> dict:
    if LOCAL:
        text = await _ollama_complete(messages)
    else:
        text = await _groq_complete(messages)
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


async def _groq_complete(messages: list[dict]) -> str:
    resp = await groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)