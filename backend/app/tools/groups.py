# Picks which tools to send with each chat message. Every tool's schema costs tokens on every
# request, and the free tier allows only 8K tokens a minute, so instead of sending all of them the
# tools are grouped, and a message gets:
#   - the core group, always (small, and needed anywhere);
#   - groups whose example phrases are similar to the message (its embedding is already computed
#     for memory search, so this costs no extra calls);
#   - groups similar to the previous exchange (its stored embedding), so a follow-up like "yes,
#     do it" after "want me to set a reminder?" still has the reminder tools;
#   - groups whose tools ran in the last RECENT_TURNS exchanges ("the second one", "make it 5pm").
# Each group also carries its own prompt rules, so those are only sent when the group is.
# If the example embeddings aren't available (Ollama down at startup), every tool is offered.
import asyncio
from dataclasses import dataclass

from app.core.logs import get_logger
from app.llm.embeddings import cosine_similarity, embed_many

log = get_logger("tools")

MESSAGE_MIN = 0.68  # message vs. example phrase similarity needed to include a group
BEST_MIN = 0.58  # the single best-matching group is included from this lower score
EXCHANGE_MIN = 0.68  # previous exchange vs. example phrase (document vs. query embedding)
RECENT_TURNS = 2


@dataclass
class Group:
    name: str
    tools: tuple[str, ...]
    rules: str
    examples: tuple[str, ...]


CORE_RULES = """Use a tool only when the message needs it; otherwise answer directly. The date and the
user's local time are below, so they never need a tool. Only remember or forget when asked; if
forget returns candidates, ask which one, never guess; after remember or forget, say what changed.
Mention sources after a web search. Reply only after a tool's result arrives. If something needs a
tool you don't have here, say you can't do that right now. Never make up results, news, papers or
links."""

GROUPS = (
    # web_search is core: open-ended lookups ("population of Japan") are too varied to match reliably,
    # and a missed search means an answer from stale knowledge
    Group("core", ("get_current_time", "remember", "forget", "web_search"), CORE_RULES, ()),
    Group("weather", ("get_weather",),
          "Weather with no place given: use the user's location from the facts, or ask.",
          ("what's the weather", "will it rain today", "do I need a jacket", "how hot is it outside",
           "forecast for this weekend", "is it going to snow", "temperature in Chicago",
           "should I bring an umbrella", "how cold will it be tonight", "weather tomorrow")),
    Group("reminders", ("remind_me", "list_reminders", "cancel_reminder"),
          ("Only cancel a reminder when asked; if cancel_reminder returns candidates, ask which one. "
           "remind_me: in_minutes for relative times, due_at for specific ones; ask if the time is unclear."),
          ("remind me to call mom", "remind me in 20 minutes", "set a reminder for tomorrow at 3pm",
           "what reminders do I have", "cancel my reminder", "don't let me forget to",
           "ping me at 5pm", "delete the dentist reminder", "move my reminder to 6",
           "nudge me in an hour", "do I have any reminders today")),
    Group("brief", ("get_morning_brief", "list_brief_sections", "add_brief_section", "change_brief_section"),
          ("Morning brief: get_morning_brief to show it (never write one yourself); the other brief tools "
           "change it. For a feed, use a known RSS/Atom url (web_search for one if unsure)."),
          ("what's in my morning brief", "get me my morning brief", "add arXiv papers to my brief",
           "add Hacker News to my morning brief", "remove the weather from my brief",
           "put the papers first in the brief", "show me my daily brief", "add this RSS feed",
           "include the latest AI papers every morning", "change my brief", "what's in today's brief",
           "add a subreddit to my brief")),
    Group("homelab", ("homelab_stats",), "",
          ("how is my Pi doing", "is the server healthy", "check the Raspberry Pi temperature",
           "is my home server ok", "disk space on the server", "is nginx running",
           "how much memory is the Pi using", "is the Pi throttling", "server stats", "CPU load",
           "check if ollama is still running", "is the assistant service up", "are all my services running",
           "is cloudflared down", "is nginx up", "how long has the server been up")),
)
# The group a tool belongs to, for "used recently"
_owner: dict[str, str] = {}
for _group in GROUPS:
    for _tool in _group.tools:
        _owner.setdefault(_tool, _group.name)

_example_vecs: dict[str, list[list[float]]] | None = None
_lock = asyncio.Lock()


async def load() -> bool:
    """Embeds every group's example phrases once (one Ollama call); at startup, or on first use."""
    global _example_vecs
    async with _lock:
        if _example_vecs is None:
            try:
                phrases = [(g.name, p) for g in GROUPS for p in g.examples]
                vecs = await embed_many([p for _, p in phrases], "query")
                loaded: dict[str, list[list[float]]] = {}
                for (name, _), vec in zip(phrases, vecs):
                    loaded.setdefault(name, []).append(vec)
                _example_vecs = loaded
                log.info("tool groups ready: %d example phrases embedded", len(phrases))
            except Exception:
                log.warning("couldn't embed tool group examples; offering every tool", exc_info=True)
        return _example_vecs is not None


def _scores(vec: list[float] | None) -> dict[str, float]:
    if not vec or not _example_vecs:
        return {}
    return {name: max(cosine_similarity(vec, v) for v in vecs) for name, vecs in _example_vecs.items()}


@dataclass
class Selection:
    tools: set[str] | None  # None = every tool (no embeddings to choose with)
    rules: str
    reasons: list[str]


async def select(message_vec: list[float] | None, last_exchange_vec: list[float] | None,
                 recent: list[dict]) -> Selection:
    """The tools and prompt rules for one message. `recent` is the conversation's recent exchanges
    (oldest first), with the tool_calls saved on each."""
    if not await load():
        return Selection(None, "\n".join(g.rules for g in GROUPS if g.rules), ["all (no embeddings)"])
    chosen: dict[str, str] = {"core": "always"}
    scores = _scores(message_vec)
    best = max(scores, key=scores.get, default=None)
    for name, score in scores.items():
        if score >= MESSAGE_MIN or (name == best and score >= BEST_MIN):
            chosen.setdefault(name, f"{score:.2f}")
    for name, score in _scores(last_exchange_vec).items():
        if score >= EXCHANGE_MIN:
            chosen.setdefault(name, f"previous turn {score:.2f}")
    for exchange in recent[-RECENT_TURNS:]:
        for call in exchange.get("tool_calls") or []:
            if name := _owner.get(call.get("name")):
                chosen.setdefault(name, "used recently")
    groups = [g for g in GROUPS if g.name in chosen]
    return Selection({t for g in groups for t in g.tools},
                     "\n".join(g.rules for g in groups if g.rules),
                     [f"{g.name} ({chosen[g.name]})" for g in groups])
