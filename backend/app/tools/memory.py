# Memory tools: remember (saves a fact, merging with existing ones) and forget (deletes only a clear
# match, otherwise asks).
from app import db
from app.core.config import EMBED_MODEL
from app.llm.embeddings import embed
from app.memory import writer as memory_writer
from app.tools.registry import ToolContext, registry

# forget deletes on its own only when the best match is this similar to the request
# and clearly ahead of the runner-up; otherwise it hands candidates back to the model.
# Measured with nomic-embed-text: correct matches score ~0.6-0.8 with a gap of 0.15+ to the
# next fact, while ambiguous requests ("my job" with two jobs stored) have gaps under 0.06.
# The gap is the stronger signal: "where I work" scored "lives in Atlanta" highest.
FORGET_CONFIDENT = 0.6
FORGET_MARGIN = 0.1
FORGET_CANDIDATE_MIN = 0.5
FORGET_CANDIDATES = 5

# Tools that change memory: the background fact extractor is skipped after a turn that used one
MEMORY_TOOLS = {"remember", "forget"}


def _after(prefix: str, result: str) -> str:
    return result.split(prefix, 1)[1].strip() if prefix in result else result


def _summarize_remember(result: str) -> tuple[str, str]:
    for prefix, summary in (("Saved:", "Saved to memory"), ("Updated:", "Updated memory"),
                            ("Already known:", "Already in memory"),
                            ("Removed outdated fact:", "Removed an outdated memory")):
        if result.startswith(prefix):
            return summary, _after(prefix, result)
    return "Didn't save to memory", result


def _summarize_forget(result: str) -> tuple[str, str]:
    if result.startswith("Forgot:"):
        return "Forgot", _after("Forgot:", result)
    if result.startswith("Not sure"):
        # Show the candidate facts, not the instructions and ids meant for the model
        candidates = [line.removeprefix("- ") for line in result.splitlines() if line.startswith("- ")]
        return "Asked which memory to forget", "\n".join(candidates)
    return "Nothing matched to forget", result


@registry.tool(
    description="Save a lasting fact about the user (only when asked). Merges with or skips existing facts.",
    parameters={
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "Short third-person sentence, e.g. 'The user has a dog named Max.'"},
            "category": {"type": "string", "enum": ["personal", "work", "preference", "project", "goal", "other"]},
        },
        "required": ["fact"],
    },
    running="Saving to memory",
    summarize=_summarize_remember,
)
async def remember(ctx: ToolContext, fact: str, category: str = "other") -> str:
    fact = fact.strip()
    if not fact:
        return "Error: fact is empty"
    return await memory_writer.reconcile(
        ctx.user_id, memory_writer.ExtractedFact(text=fact, category=category), ctx.conversation_id)


@registry.tool(
    description=("Delete a stored fact (only when asked). Deletes only a clear match; otherwise returns "
                 "candidates to ask about, then call again with the chosen fact's exact text."),
    parameters={
        "type": "object",
        "properties": {"description": {"type": "string", "description": "What to forget, e.g. 'my dog's name'."}},
        "required": ["description"],
    },
    running="Looking for the memory to forget",
    summarize=_summarize_forget,
)
async def forget(ctx: ToolContext, description: str) -> str:
    if not description.strip():
        return "Error: describe the fact to forget."
    vec = await embed(description, "query")
    matches = await db.search_facts(ctx.user_id, vec, EMBED_MODEL, FORGET_CANDIDATES, FORGET_CANDIDATE_MIN)
    if not matches:
        return f"No stored fact matches {description!r}. Nothing was deleted."

    # The follow-up after a "which one?" passes a candidate's exact text
    exact = [m for m in matches if m["text"].strip().lower() == description.strip().lower()]
    best = exact[0] if exact else matches[0]
    runner_up = matches[1]["score"] if len(matches) > 1 else 0.0
    if exact or (best["score"] >= FORGET_CONFIDENT and best["score"] - runner_up >= FORGET_MARGIN):
        await db.delete_fact(ctx.user_id, best["id"])
        return f"Forgot: {best['text']}"

    # No ids in the listing: whatever the model sees, it may repeat to the user
    listing = "\n".join(f"- {m['text']}" for m in matches)
    return ("Not sure which fact is meant, so nothing was deleted. Ask the user which one to forget "
            f"(or whether none of these), then call forget again with that fact's exact text:\n{listing}")
