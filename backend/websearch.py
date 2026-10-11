# Web search via the Brave Search API (needs BRAVE_API_KEY). The web_search tool is only offered
# to the model when the key is set.
import html
import re

import httpx

from config import BRAVE_API_KEY

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_RESULTS = 5
TAG = re.compile(r"<[^>]+>")  # Brave marks matched words with <strong>


def _clean(text: str) -> str:
    return html.unescape(TAG.sub("", text or "")).strip()


async def search(query: str) -> str:
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        resp = await client.get(
            BRAVE_URL,
            params={"q": query, "count": MAX_RESULTS},
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
        )
        if resp.status_code == 429:
            return "Error: Brave Search rate limit reached (free plan: 1 query/second, monthly cap). Try again shortly."
        resp.raise_for_status()
        data = resp.json()
    results = (data.get("web") or {}).get("results") or []
    lines = []
    for r in results[:MAX_RESULTS]:
        age = f" ({r['age']})" if r.get("age") else ""
        lines.append(f"- {_clean(r.get('title'))}{age} — {r.get('url', '')}\n  {_clean(r.get('description'))}")
    return "\n".join(lines) or f"No results for {query!r}."
