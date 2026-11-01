# FastAPI router for conversation and chat endpoints: create/list/delete conversations,
# save/retrieve exchanges, and stream LLM replies with semantic memory retrieval injected into context.
import asyncio
import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from groq import RateLimitError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import db2
from embeddings import embed, cosine_similarity
from models import ExchangeIn
from config import (ALL_FACTS_LIMIT, EMBED_MODEL, EXCERPT_CHARS, FACT_TOP_K, HISTORY_TOKENS, HISTORY_TURNS,
                    LATEST_REPLY_CHARS, LOCAL, MIN_SIMILARITY, OLDER_REPLY_CHARS, SYSTEM_PROMPT, TOOLS_PROMPT,
                    TOP_K, USER_TIMEZONE, VOICE_PROMPT)
from embeddings import embed
from llm import chat_stream
from models import ChatRequest, TitleUpdate
import memory_writer
import retrieval
import titles
import usertime
from logs import get_logger
import tools
from auth import current_user



log = get_logger("chat")

router = APIRouter(prefix="/Assistant/api/conversations", tags=["conversations"],
                   dependencies=[Depends(current_user)])

@router.post("")
async def new_conversation(user: dict = Depends(current_user)):
    return await db2.create_conversation(user["id"])

@router.get("")
async def get_conversations(user: dict = Depends(current_user)):
    return await db2.list_conversations(user["id"])

@router.post("/{conversation_id}/exchanges")
async def save_exchange(conversation_id: str, body: ExchangeIn,
                        user: dict = Depends(current_user)):
    if not await db2.get_conversation(user["id"], conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    text = f"User: {body.user_text}\nAssistant: {body.assistant_text}"
    vec = await embed(text, "document")
    eid = await db2.add_exchange(user["id"], conversation_id, body.user_text, body.assistant_text, vec, EMBED_MODEL)
    return {"id": eid}

@router.get("/{conversation_id}/exchanges")
async def get_exchanges(conversation_id: str, user: dict = Depends(current_user)):
    return await db2.list_exchanges(user["id"], conversation_id)

def build_messages(recent: list[dict], facts: list[dict], all_facts: bool, memories: list[dict],
                   user_text: str, tz: ZoneInfo | None = None, voice: bool = False) -> list[dict]:
    """tz is the user's own timezone (usertime.py); defaults to USER_TIMEZONE. voice: the reply
    will be read aloud, so ask for short spoken sentences."""
    system = SYSTEM_PROMPT
    if voice:
        system += "\n\n" + VOICE_PROMPT
    if not LOCAL:
        system += "\n\n" + TOOLS_PROMPT
    now = datetime.now(tz or ZoneInfo(USER_TIMEZONE))
    system += f"\n\nToday is {now:%A, %B %-d, %Y}. The user's local time is {now:%-I:%M %p %Z}."
    if all_facts:
        if facts:
            system += ("\n\nEverything you know about the user (the complete list; if asked what you "
                       "know about them, share all of it):\n"
                       + "\n".join(f"- {f['text']}" for f in facts))
        else:
            system += "\n\nYou don't know any facts about the user yet."
    elif facts:
        system += ("\n\nKnown facts about the user (only the ones most relevant to this message; "
                   "more are stored):\n" + "\n".join(f"- {f['text']}" for f in facts))
    if memories:
        system += "\n\n" + format_memories(memories)

    messages = [{"role": "system", "content": system}]
    for user_turn, assistant_turn in fit_history(recent):
        if user_turn:  # empty for assistant-initiated messages like the morning brief
            messages.append({"role": "user", "content": user_turn})
        messages.append({"role": "assistant", "content": assistant_turn})
    messages.append({"role": "user", "content": user_text})
    return messages


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " …[trimmed]"


def fit_history(recent: list[dict]) -> list[tuple[str, str]]:
    """Recent turns, newest kept most fully, within about HISTORY_TOKENS. Long old replies (tables,
    code) are what blew past Groq's 8K tokens/minute: they were re-sent with every message."""
    budget, used, fitted = HISTORY_TOKENS * 4, 0, []
    for age, ex in enumerate(reversed(recent)):
        user_turn = clip(ex["user_text"], 1000)
        assistant_turn = clip(ex["assistant_text"], LATEST_REPLY_CHARS if age == 0 else OLDER_REPLY_CHARS)
        cost = len(user_turn) + len(assistant_turn)
        if fitted and used + cost > budget:
            break
        fitted.append((user_turn, assistant_turn))
        used += cost
    return list(reversed(fitted))


@router.post("/{conversation_id}/chat")
async def chat(conversation_id: str, req: ChatRequest, request: Request,
               user: dict = Depends(current_user)):
    started = time.perf_counter()
    user_id = user["id"]
    # Independent lookups run at the same time. Facts: one past the limit, to learn whether the
    # user has more facts than fit in the prompt.
    conversation, history, query_vec, facts = await asyncio.gather(
        db2.get_conversation(user_id, conversation_id),
        db2.list_exchanges(user_id, conversation_id),
        embed(req.message, "query"),
        db2.list_facts(user_id, limit=ALL_FACTS_LIMIT + 1),
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    recent = history[-HISTORY_TURNS:]
    all_facts = len(facts) <= ALL_FACTS_LIMIT

    # Untitled chats get a title from their first message, generated while the reply streams
    title_task = None
    if conversation["title"] is None:
        first_message = history[0]["user_text"] if history else req.message
        title_task = asyncio.create_task(titles.generate(first_message))

    # Second wave, also in parallel: memory search, the user's timezone (reusing the facts just
    # loaded when they're all there), and the relevant facts if there are too many to send them all
    memories, tz, relevant_facts = await asyncio.gather(
        retrieval.recall_exchanges(user_id, req.message, query_vec, top_k=TOP_K, min_similarity=MIN_SIMILARITY,
                                   exclude_ids=[ex["id"] for ex in recent]),
        usertime.user_zone(user_id, facts if all_facts else None),
        retrieval.recall_facts(user_id, req.message, query_vec, FACT_TOP_K, MIN_SIMILARITY)
        if not all_facts else asyncio.sleep(0, result=None),
    )
    if not all_facts:
        facts = relevant_facts

    log.debug("context: %d recent turns, %d facts (all=%s), %d past exchanges",
              len(recent), len(facts), all_facts, len(memories))
    messages = build_messages(recent, facts, all_facts, memories, req.message, tz, req.voice)
    setup_seconds = time.perf_counter() - started

    ctx = tools.ToolContext(user_id=user_id, conversation_id=conversation_id, username=user["username"], tz=tz)

    # Streams newline-delimited JSON events:
    #   {"type": "text", "text": ...}       a piece of the reply
    #   {"type": "tool_start", ...}         a tool began (id, name, label)
    #   {"type": "tool_end", ...}           a tool finished (id, name, ok, summary, detail)
    #   {"type": "retract", "chars": n}     drop the last n characters of text (a pre-tool draft)
    #   {"type": "title", "title": ...}     the conversation was given an auto-generated title
    #   {"type": "error", "message": ...}   generation failed; nothing is saved
    # Only clients that ask for it (Accept: application/x-ndjson) get events. Anything else, such
    # as a browser tab still running the frontend from before a deploy, gets the reply as plain text.
    ndjson = "application/x-ndjson" in request.headers.get("accept", "")

    def event(data: dict) -> str:
        if ndjson:
            return json.dumps(data) + "\n"
        if data["type"] == "text":
            return data["text"]
        if data["type"] == "retract":
            return "\n\n"  # plain text can't be taken back; at least separate the drafts
        return "\n\n[Something went wrong generating a reply.]" if data["type"] == "error" else ""

    async def generate():
        reply, tool_calls, first_text = "", [], None
        try:
            async for piece in chat_stream(messages, tools=tools.registry, ctx=ctx):
                if isinstance(piece, str):
                    if first_text is None:
                        first_text = time.perf_counter() - started
                    reply += piece
                    yield event({"type": "text", "text": piece})
                else:
                    if piece["type"] == "retract":
                        reply = reply[: max(0, len(reply) - piece["chars"])]
                    if piece["type"] == "tool_end":
                        tool_calls.append({k: piece[k] for k in ("name", "ok", "summary", "detail")})
                    if out := event(piece):
                        yield out
        except RateLimitError as e:
            log.warning("Groq rate limit: %s", e)
            # Groq says when to retry, e.g. "Please try again in 7m38.784s"
            wait = re.search(r"try again in ((?:\d+h)?(?:\d+m)?)([\d.]+s)?", str(e))
            if wait and wait.group(1):
                when = f" Try again in about {wait.group(1)}."
            elif wait:
                when = " Try again in a few seconds."
            else:
                when = " Try again later."
            yield event({"type": "error", "message": "The AI service's usage limit was reached." + when})
            if title_task:
                title_task.cancel()
            return
        except Exception:
            log.exception("reply generation failed")
            yield event({"type": "error", "message": "Something went wrong generating a reply."})
            if title_task:
                title_task.cancel()
            return

        if title_task and (title := await title_task):
            # Skipped if the user renamed the chat while the reply was streaming
            if await db2.set_title_if_empty(user_id, conversation_id, title):
                log.info("titled conversation %s: %s", conversation_id, title)
                if out := event({"type": "title", "title": title}):
                    yield out

        log.info("timing: first text after %s, done in %.1fs (setup %.0fms, tools %.1fs%s)",
                 f"{first_text:.1f}s" if first_text is not None else "-", time.perf_counter() - started,
                 setup_seconds * 1000, ctx.tool_seconds, f", {', '.join(ctx.calls)}" if ctx.calls else "")
        # Saving happens after the stream closes, so the user can type again right away
        _spawn(_save_exchange(user_id, conversation_id, req.message, reply.strip(), tool_calls, list(ctx.calls)))

    return StreamingResponse(generate(), media_type="application/x-ndjson" if ndjson else "text/plain")


_background: set[asyncio.Task] = set()  # keeps post-reply saves alive until they finish


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _save_exchange(user_id: str, conversation_id: str, user_text: str, reply: str,
                         tool_calls: list[dict], calls: list[str]) -> None:
    """Embeds and stores the exchange, then starts memory extraction. Never raises."""
    try:
        vec = await embed(f"User: {user_text}\nAssistant: {reply}", "document")
        await db2.add_exchange(user_id, conversation_id, user_text, reply, vec, EMBED_MODEL, tool_calls)
    except Exception:
        log.exception("saving the exchange failed (conversation %s)", conversation_id)
        return
    # An explicit remember/forget already updated memory; running the background extractor on
    # the same message could undo it (e.g. saving "The user wants to forget X" after a forget)
    if tools.MEMORY_TOOLS.intersection(calls):
        log.info("skipping background extraction: memory tools ran this turn")
    else:
        memory_writer.schedule(user_id, conversation_id, user_text, reply)

@router.patch("/{conversation_id}")
async def rename_conversation(conversation_id: str, body: TitleUpdate,
                              user: dict = Depends(current_user)):
    # An empty title clears it, so the UI falls back to showing the date
    conv = await db2.rename_conversation(user["id"], conversation_id, body.title.strip() or None)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, user: dict = Depends(current_user)):
    if not await db2.delete_conversation(user["id"], conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")

def format_memories(memories: list[dict]) -> str:
    lines = ["Relevant excerpts from past conversations with this user:"]
    for m in memories:
        date = m["created_at"][:10]
        lines.append(f"\n[{date}]\nUser: {clip(m['user_text'], EXCERPT_CHARS)}\n"
                     f"Assistant: {clip(m['assistant_text'], EXCERPT_CHARS)}")
    return "\n".join(lines)

# def build_messages(recent: list[dict], memories: list[dict], user_text: str) -> list[dict]:
#     system = SYSTEM_PROMPT
#     if memories:
#         system += "\n\n" + format_memories(memories)

#     messages = [{"role": "system", "content": system}]
#     for ex in recent:
#         messages.append({"role": "user", "content": ex["user_text"]})
#         messages.append({"role": "assistant", "content": ex["assistant_text"]})
#     messages.append({"role": "user", "content": user_text})
#     return messages
