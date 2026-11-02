# web_search: Brave Search results. Only offered when BRAVE_API_KEY is set.
from app.core.config import BRAVE_API_KEY
from app.services import websearch
from app.tools.registry import ToolContext, registry


@registry.tool(
    description="Search the web for current information. Returns top results with URLs.",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    running="Searching the web",
    summarize=lambda result: ("Searched the web", result),
    available=lambda ctx: bool(BRAVE_API_KEY),
)
async def web_search(ctx: ToolContext, query: str) -> str:
    if not query.strip():
        return "Error: query is empty"
    return await websearch.search(query)
