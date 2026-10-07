import asyncio
from typing import Literal

from pydantic import BaseModel, ValidationError

import db2
from config import EMBED_MODEL
from embeddings import embed
from llm import complete_json

SIMILAR_FACTS = 5
RELATED_THRESHOLD = 0.6
DUPLICATE_THRESHOLD = 0.95

EXTRACT_PROMPT = """You extract durable facts about the USER from one exchange of a conversation.

Rules:
- Only include facts the user stated directly or clearly confirmed. Never guess or infer.
- Never take facts from the assistant's reply. It is shown only as context.
- Durable means likely still true in a month: identity, job, relationships, pets,
  preferences, projects, goals, skills, location.
- Skip questions, one-off requests, small talk, and details only about the current task.
- Skip sensitive data: passwords, account numbers, government IDs, health conditions.
- Write each fact as one short third-person sentence, e.g. "The user has a dog named Max."
- One fact per item. Most messages contain no facts; return an empty list then.

Respond with JSON only, in this shape:
{"facts": [{"text": "...", "category": "personal|work|preference|project|goal|other"}]}"""

DECIDE_PROMPT = """You maintain a list of facts about a user. Compare a NEW fact with the
numbered EXISTING facts and choose exactly one action:

- ADD: the new fact is information not covered by any existing fact.
- UPDATE: the new fact changes or adds detail to one existing fact. Set "target" to
  that fact's number and "text" to the merged, up-to-date fact.
- DELETE: the new fact says an existing fact is no longer true and there is nothing
  new to store. Set "target" to that fact's number.
- NONE: the new fact is already covered by an existing fact.

Prefer UPDATE over ADD when both facts are about the same thing (the same pet, job, city).

Respond with JSON only:
{"action": "ADD|UPDATE|DELETE|NONE", "target": <number or null>, "text": "<fact or null>"}"""


class ExtractedFact(BaseModel):
    text: str
    category: str = "other"


class Extraction(BaseModel):
    facts: list[ExtractedFact] = []


class Decision(BaseModel):
    action: Literal["ADD", "UPDATE", "DELETE", "NONE"]
    target: int | None = None
    text: str | None = None


_tasks: set[asyncio.Task] = set()


def schedule(user_id: str, conversation_id: str, user_text: str, assistant_text: str):
    task = asyncio.create_task(process_exchange(user_id, conversation_id, user_text, assistant_text))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def process_exchange(user_id: str, conversation_id: str, user_text: str, assistant_text: str):
    try:
        for fact in await extract_facts(user_text, assistant_text):
            await reconcile(user_id, fact, conversation_id)
    except Exception as e:
        print(f"[memory] failed: {e!r}")


async def extract_facts(user_text: str, assistant_text: str) -> list[ExtractedFact]:
    messages = [
        {"role": "system", "content": EXTRACT_PROMPT},
        {"role": "user", "content": f"User: {user_text}\nAssistant: {assistant_text}"},
    ]
    data = await complete_json(messages)
    try:
        return Extraction.model_validate(data).facts
    except ValidationError as e:
        print(f"[memory] bad extraction output: {e}")
        return []


async def reconcile(user_id: str, fact: ExtractedFact, conversation_id: str):
    vec = await embed(fact.text, "document")
    similar = await db2.search_facts(user_id, vec, EMBED_MODEL, SIMILAR_FACTS, RELATED_THRESHOLD)

    if not similar:
        await db2.add_fact(user_id, fact.text, fact.category, vec, EMBED_MODEL, conversation_id)
        print(f"[memory] ADD: {fact.text}")
        return

    if similar[0]["score"] >= DUPLICATE_THRESHOLD:
        print(f"[memory] NONE (duplicate): {fact.text}")
        return

    decision = await decide(fact, similar)
    await apply(user_id, decision, fact, vec, similar, conversation_id)


async def decide(fact: ExtractedFact, similar: list[dict]) -> Decision:
    listing = "\n".join(f"{i}. {f['text']}" for i, f in enumerate(similar, start=1))
    messages = [
        {"role": "system", "content": DECIDE_PROMPT},
        {"role": "user", "content": f"EXISTING facts:\n{listing}\n\nNEW fact: {fact.text}"},
    ]
    data = await complete_json(messages)
    try:
        return Decision.model_validate(data)
    except ValidationError as e:
        print(f"[memory] bad decision output: {e}")
        return Decision(action="NONE")


async def apply(user_id: str, decision: Decision, fact: ExtractedFact, vec: list[float],
                similar: list[dict], conversation_id: str):
    target = None
    if decision.action in ("UPDATE", "DELETE"):
        if decision.target is None or not 1 <= decision.target <= len(similar):
            print(f"[memory] invalid target {decision.target}, skipping: {fact.text}")
            return
        target = similar[decision.target - 1]

    text = (decision.text or fact.text).strip()
    if text != fact.text:
        vec = await embed(text, "document")

    if decision.action == "ADD":
        await db2.add_fact(user_id, text, fact.category, vec, EMBED_MODEL, conversation_id)
        print(f"[memory] ADD: {text}")
    elif decision.action == "UPDATE":
        await db2.update_fact(user_id, target["id"], text, vec)
        print(f"[memory] UPDATE: {target['text']} -> {text}")
    elif decision.action == "DELETE":
        await db2.delete_fact(user_id, target["id"])
        print(f"[memory] DELETE: {target['text']}")
    else:
        print(f"[memory] NONE: {fact.text}")