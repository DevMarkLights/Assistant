# Short auto-generated conversation titles, made from the conversation's first message with the
# (cheaper) MEMORY_MODEL while the reply streams.
from config import MEMORY_MODEL
from llm import complete_json
from logs import get_logger

log = get_logger("titles")

MAX_LENGTH = 60
TITLE_PROMPT = """Write a short title (2 to 6 words) for a chat that starts with the user's message
below. Describe the topic, like a sidebar label: no quotes, no trailing punctuation, no "Chat about".
Respond with JSON only: {"title": "..."}"""


async def generate(first_message: str) -> str | None:
    messages = [
        {"role": "system", "content": TITLE_PROMPT},
        {"role": "user", "content": first_message[:1000]},
    ]
    try:
        data = await complete_json(messages, model=MEMORY_MODEL, purpose="title")
    except Exception:
        log.warning("title generation failed", exc_info=True)
        return None
    title = str(data.get("title") or "").strip().strip("\"'").rstrip(".!?:").strip()
    return title[:MAX_LENGTH] or None
