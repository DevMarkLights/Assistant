# Morning brief tools: the user changes what their brief contains by asking ("add the latest AI
# papers from arXiv", "drop the weather", "put the papers first"). Sections are stored as data
# (services/brief_sections.py), so no new code is needed for a new kind of content.
from datetime import datetime

from app import db
from app.services import brief, brief_sections
from app.tools.registry import ToolContext, registry


def _numbered(sections: list[dict]) -> str:
    return "\n".join(f"{i}. {brief_sections.describe(s)}" for i, s in enumerate(sections, 1))


def _summarize_list(result: str) -> tuple[str, str]:
    return "Checked your morning brief", result.removeprefix("Morning brief sections, in order:\n")


@registry.tool(
    description="Add a morning brief section. feed: any RSS/Atom url (arXiv: https://rss.arxiv.org/rss/cs.AI).",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "source": {"type": "string", "enum": list(brief_sections.SOURCE_TYPES)},
            "url": {"type": "string"},
            "query": {"type": "string"},
            "location": {"type": "string", "description": "Omit for home."},
            "instructions": {"type": "string", "description": "How to write it."},
            "position": {"type": "integer", "description": "1 = first."},
        },
        "required": ["title", "source"],
    },
    running="Updating your morning brief",
    summarize=lambda result: ("Added to your morning brief", result.removeprefix("Added: ")),
)
async def add_brief_section(ctx: ToolContext, title: str, source: str, url: str | None = None,
                            query: str | None = None, location: str | None = None,
                            instructions: str = "", position: int | None = None) -> str:
    sections = await brief_sections.sections_for(ctx.user_id, ctx.username)
    if len(sections) >= brief_sections.MAX_SECTIONS:
        return f"Error: the brief already has {brief_sections.MAX_SECTIONS} sections; remove one first"
    if not title.strip():
        return "Error: title is empty"
    spec = {"type": source, "url": (url or "").strip(), "query": (query or "").strip(),
            "location": (location or "").strip()}
    spec = {k: v for k, v in spec.items() if v}
    error, note = await brief_sections.check_source(ctx.username, spec)
    if error:
        return error
    section = await db.insert_brief_section(ctx.user_id, title.strip(), spec, instructions.strip())
    if position is not None:
        ids = [s["id"] for s in sections]
        ids.insert(max(0, min(position - 1, len(ids))), section["id"])
        await db.set_brief_section_order(ctx.user_id, ids)
    return f"Added: {brief_sections.describe(section)}" + (f"\n{note}" if note else "") + \
        "\nIt appears in tomorrow's brief."


@registry.tool(
    description="Get the user's morning brief now: fetches every section's current content.",
    parameters={"type": "object", "properties": {}},
    running="Putting together your brief",
    summarize=lambda result: ("Got your morning brief", f"{result.count(chr(10) + '## ')} sections"),
)
async def get_morning_brief(ctx: ToolContext) -> str:
    notes = await brief.gather({"id": ctx.user_id, "username": ctx.username}, datetime.now(ctx.tz))
    return ("Write the brief from these sections only, in order, following each 'How to write it'. Keep every "
            "item's Markdown link. Never add items that aren't here.\n\n" + brief.notes_text(notes))


@registry.tool(
    description="List the morning brief's sections.",
    parameters={"type": "object", "properties": {}},
    running="Checking your morning brief",
    summarize=_summarize_list,
)
async def list_brief_sections(ctx: ToolContext) -> str:
    sections = await brief_sections.sections_for(ctx.user_id, ctx.username)
    if not sections:
        return "The morning brief has no sections; it only gives the date."
    return "Morning brief sections, in order:\n" + _numbered(sections)


@registry.tool(
    description="Remove, move, or edit a morning brief section; returns candidates if unclear.",
    parameters={
        "type": "object",
        "properties": {
            "section": {"type": "string", "description": "Title or list number."},
            "action": {"type": "string", "enum": ["remove", "move", "edit"]},
            "position": {"type": "integer"},
            "title": {"type": "string"},
            "instructions": {"type": "string"},
        },
        "required": ["section", "action"],
    },
    running="Updating your morning brief",
    summarize=lambda result: ("Updated your morning brief", result),
)
async def change_brief_section(ctx: ToolContext, section: str, action: str, position: int | None = None,
                               title: str | None = None, instructions: str | None = None) -> str:
    sections = await brief_sections.sections_for(ctx.user_id, ctx.username)
    target, candidates = brief_sections.find(sections, section)
    if not target:
        return f"Not sure which section {section!r} means. Ask the user which one:\n" + _numbered(candidates)
    if action == "remove":
        await db.delete_brief_section(ctx.user_id, target["id"])
        return f"Removed {target['title']!r} from the morning brief."
    if action == "move":
        if position is None:
            return "Error: move needs a position"
        ids = [s["id"] for s in sections if s["id"] != target["id"]]
        ids.insert(max(0, min(position - 1, len(ids))), target["id"])
        await db.set_brief_section_order(ctx.user_id, ids)
        return f"Moved {target['title']!r} to position {ids.index(target['id']) + 1}."
    if action == "edit":
        fields = {k: v.strip() for k, v in (("title", title), ("instructions", instructions)) if v is not None}
        if not fields or ("title" in fields and not fields["title"]):
            return "Error: edit needs a new title or instructions"
        await db.update_brief_section(ctx.user_id, target["id"], fields)
        return f"Updated {target['title']!r}: " + ", ".join(f"{k} = {v!r}" for k, v in fields.items())
    return "Error: action must be remove, move, or edit"
