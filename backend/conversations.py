# FastAPI router for conversation and chat endpoints: create/list/delete conversations,
# save/retrieve exchanges, and stream LLM replies with semantic memory retrieval injected into context.
import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

import db2
from embeddings import embed, cosine_similarity
from models import ExchangeIn
from config import (ALL_FACTS_LIMIT, EMBED_MODEL, FACT_TOP_K, HISTORY_TURNS, MIN_SIMILARITY,
                    SYSTEM_PROMPT, TOP_K)
from embeddings import embed
from llm import chat_stream
from models import ChatRequest, TitleUpdate
import memory_writer
from auth import current_user



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
async def chat(conversation_id: str, req: ChatRequest, user: dict = Depends(current_user)):
    user_id = user["id"]
    if not await db2.get_conversation(user_id, conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")

    history = await db2.list_exchanges(user_id, conversation_id)
    recent = history[-HISTORY_TURNS:]

    query_vec = await embed(req.message, "query")
    memories = await db2.search_exchanges(
        user_id,
        query_vec,
        EMBED_MODEL,
        top_k=TOP_K,
        min_similarity=MIN_SIMILARITY,
        exclude_ids=[ex["id"] for ex in recent],
    )

    # Fetch one past the limit to learn whether the user has more facts than fit
    facts = await db2.list_facts(user_id, limit=ALL_FACTS_LIMIT + 1)
    all_facts = len(facts) <= ALL_FACTS_LIMIT
    if not all_facts:
        facts = await db2.search_facts(user_id, query_vec, EMBED_MODEL, FACT_TOP_K, MIN_SIMILARITY)
        for f in facts:
            print(f"fact {f['score']:.3f}: {f['text']}")

    messages = build_messages(recent, facts, all_facts, memories, req.message)

    async def generate():
        parts = []
        async for piece in chat_stream(messages):
            parts.append(piece)
            yield piece

        reply = "".join(parts).strip()
        vec = await embed(f"User: {req.message}\nAssistant: {reply}", "document")
        await db2.add_exchange(user_id, conversation_id, req.message, reply, vec, EMBED_MODEL)
        memory_writer.schedule(user_id, conversation_id, req.message, reply)

    return StreamingResponse(generate(), media_type="text/plain")

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
