# Builds what the chat model sees for a message: the system prompt (with tool guidance, today's date
# in the user's timezone, known facts, and past-conversation excerpts) plus trimmed recent history.
# The prompts live here, next to the code that uses them.
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import (EXCERPT_CHARS, HISTORY_TOKENS, LATEST_REPLY_CHARS, LOCAL, OLDER_REPLY_CHARS,
                             USER_TIMEZONE)

SYSTEM_PROMPT = """You are a personal assistant running on the user's own computer.
Be concise and helpful.

You may be given known facts about the user and excerpts from past conversations.
Use them when they are relevant to the current message. Facts are the most reliable
source. Trust things the user stated directly over earlier assistant replies.
Never invent memories that are not shown to you."""

# Added when the message came from voice mode, where the reply is read aloud
VOICE_PROMPT = """The user is talking to you by voice and your reply will be read aloud. Answer in
short, natural spoken sentences: no Markdown, tables, lists, code, or URLs, and no emoji. Keep it
brief (a few sentences) unless they ask for detail. Say numbers and times the way people speak them."""

def build_messages(recent: list[dict], facts: list[dict], all_facts: bool, memories: list[dict],
                   user_text: str, tz: ZoneInfo | None = None, voice: bool = False,
                   tool_rules: str = "") -> list[dict]:
    """tz is the user's own timezone (usertime.py); defaults to USER_TIMEZONE. voice: the reply
    will be read aloud, so ask for short spoken sentences. tool_rules: guidance for the tools sent
    with this message (tools/groups.py); not used with LOCAL, where there are no tools."""
    system = SYSTEM_PROMPT
    if voice:
        system += "\n\n" + VOICE_PROMPT
    if not LOCAL and tool_rules:
        system += "\n\n" + tool_rules
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


def format_memories(memories: list[dict]) -> str:
    lines = ["Relevant excerpts from past conversations with this user:"]
    for m in memories:
        date = m["created_at"][:10]
        lines.append(f"\n[{date}]\nUser: {clip(m['user_text'], EXCERPT_CHARS)}\n"
                     f"Assistant: {clip(m['assistant_text'], EXCERPT_CHARS)}")
    return "\n".join(lines)
