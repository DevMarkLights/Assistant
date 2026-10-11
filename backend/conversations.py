# FastAPI router for conversation and chat endpoints: create/list/delete conversations,
# save/retrieve exchanges, and stream LLM replies with semantic memory retrieval injected into context.
import asyncio
import json
import os
import re
from datetime import datetime

from groq import RateLimitError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import db2
from embeddings import embed, cosine_similarity
from models import ExchangeIn
from config import (ALL_FACTS_LIMIT, EMBED_MODEL, FACT_TOP_K, HISTORY_TURNS, LOCAL, MIN_SIMILARITY,
                    SYSTEM_PROMPT, TOOLS_PROMPT, TOP_K)
from embeddings import embed
from llm import chat_stream
from models import ChatRequest, TitleUpdate
import memory_writer
import retrieval
import titles
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
                   user_text: str) -> list[dict]:
    system = SYSTEM_PROMPT
    if not LOCAL:
        system += "\n\n" + TOOLS_PROMPT
    now = datetime.now().astimezone()
    system += f"\n\nToday is {now:%A, %B %-d, %Y}. The server's local time is {now:%-I:%M %p %Z}."
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
    for ex in recent:
        messages.append({"role": "user", "content": ex["user_text"]})
        messages.append({"role": "assistant", "content": ex["assistant_text"]})
    messages.append({"role": "user", "content": user_text})
    return messages


@router.post("/{conversation_id}/chat")
async def chat(conversation_id: str, req: ChatRequest, request: Request,
               user: dict = Depends(current_user)):
    user_id = user["id"]
    conversation = await db2.get_conversation(user_id, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    history = await db2.list_exchanges(user_id, conversation_id)
    recent = history[-HISTORY_TURNS:]

    # Untitled chats get a title from their first message, generated while the reply streams
    title_task = None
    if conversation["title"] is None:
        first_message = history[0]["user_text"] if history else req.message
        title_task = asyncio.create_task(titles.generate(first_message))

    query_vec = await embed(req.message, "query")
    memories = await retrieval.recall_exchanges(
        user_id, req.message, query_vec, top_k=TOP_K, min_similarity=MIN_SIMILARITY,
        exclude_ids=[ex["id"] for ex in recent],
    )

    # Fetch one past the limit to learn whether the user has more facts than fit
    facts = await db2.list_facts(user_id, limit=ALL_FACTS_LIMIT + 1)
    all_facts = len(facts) <= ALL_FACTS_LIMIT
    if not all_facts:
        facts = await retrieval.recall_facts(user_id, req.message, query_vec, FACT_TOP_K, MIN_SIMILARITY)

    log.debug("context: %d recent turns, %d facts (all=%s), %d past exchanges",
              len(recent), len(facts), all_facts, len(memories))
    messages = build_messages(recent, facts, all_facts, memories, req.message)

    ctx = tools.ToolContext(user_id=user_id, conversation_id=conversation_id, username=user["username"])

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
        reply, tool_calls = "", []
        try:
            async for piece in chat_stream(messages, tools=tools.registry, ctx=ctx):
                if isinstance(piece, str):
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

        reply = reply.strip()
        vec = await embed(f"User: {req.message}\nAssistant: {reply}", "document")
        await db2.add_exchange(user_id, conversation_id, req.message, reply, vec, EMBED_MODEL, tool_calls)
        # An explicit remember/forget already updated memory; running the background extractor on
        # the same message could undo it (e.g. saving "The user wants to forget X" after a forget)
        if tools.MEMORY_TOOLS.intersection(ctx.calls):
            log.info("skipping background extraction: memory tools ran this turn")
        else:
            memory_writer.schedule(user_id, conversation_id, req.message, reply)

    return StreamingResponse(generate(), media_type="application/x-ndjson" if ndjson else "text/plain")

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
        lines.append(f"\n[{date}]\nUser: {m['user_text']}\nAssistant: {m['assistant_text']}")
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
