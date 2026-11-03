# Reads RSS and Atom feeds for brief sections: arXiv categories and searches, news sites, blogs,
# subreddits, YouTube channels, GitHub releases, Hacker News... Feed URLs come from users, so only
# public http(s) hosts are fetched: private, loopback and link-local addresses (this Pi's own
# services, the home network) are refused, checked again on every redirect.
import asyncio
import html
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx

MAX_BYTES = 3_000_000
MAX_REDIRECTS = 4
TIMEOUT = httpx.Timeout(15.0)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-assistant-feed-reader/1.0)"}
TAG = re.compile(r"<[^>]+>")
ARXIV_PREFIX = re.compile(r"^arXiv:\S+\s+Announce Type:\s*\S+\s+Abstract:\s*")  # arXiv RSS summaries


class FeedError(Exception):
    """A feed that can't be fetched or read; the message is shown to the user."""


async def _check_public(url: str) -> None:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FeedError("only http(s) feed URLs work")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FeedError(f"couldn't find the site {parts.hostname}") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise FeedError(f"{parts.hostname} isn't a public site")


async def fetch(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _check_public(url)
            async with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                if resp.status_code >= 400:
                    raise FeedError(f"the site answered {resp.status_code}")
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise FeedError("the feed is too large")
                return body
    raise FeedError("too many redirects")


def _name(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _clean(text: str) -> str:
    return " ".join(html.unescape(TAG.sub(" ", text or "")).split())


def _child(el, *names):
    return next((c for c in el if _name(c) in names), None)


def _text(el) -> str:
    return _clean("".join(el.itertext())) if el is not None else ""


def _link(entry) -> str:
    links = [c for c in entry if _name(c) == "link"]
    for link in links:  # Atom: <link rel="alternate" href="...">
        if link.get("href") and link.get("rel", "alternate") == "alternate":
            return link.get("href")
    for link in links:  # RSS: <link>https://...</link>
        if (link.text or "").strip():
            return link.text.strip()
    return links[0].get("href", "") if links else ""


def parse(body: bytes) -> tuple[str, list[dict]]:
    """Returns (feed title, items) for RSS 2.0, RSS 1.0/RDF or Atom; items have title, link,
    summary and date."""
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise FeedError("that address isn't an RSS or Atom feed") from None
    channel = next((el for el in root.iter() if _name(el) in ("channel", "feed")), root)
    title = _text(_child(channel, "title"))
    items = []
    for entry in root.iter():
        if _name(entry) not in ("item", "entry"):
            continue
        items.append({
            "title": _text(_child(entry, "title")) or "(untitled)",
            "link": _link(entry),
            "summary": _text(_child(entry, "description", "summary", "content", "encoded")),
            "date": _text(_child(entry, "pubDate", "published", "updated", "date")),
        })
    if not items and _name(root) not in ("rss", "feed", "RDF"):
        raise FeedError("that address isn't an RSS or Atom feed")
    return title, items


async def read(url: str) -> tuple[str, list[dict]]:
    try:
        return parse(await fetch(url))
    except httpx.HTTPError as e:
        raise FeedError(f"couldn't reach the feed ({type(e).__name__})") from None


def format_items(items: list[dict], limit: int, summary_chars: int = 110) -> str:
    """Items as Markdown lines, "- [Title](link)" plus a short summary, ready to keep as links."""
    lines = []
    for item in items[:limit]:
        summary = ARXIV_PREFIX.sub("", item["summary"])
        if len(summary) > summary_chars:
            summary = summary[:summary_chars].rsplit(" ", 1)[0] + "…"
        title = item["title"].replace("[", "(").replace("]", ")")
        heading = f"[{title}]({item['link']})" if item["link"] else title
        lines.append(f"- {heading}" + (f"\n  {summary}" if summary else ""))
    return "\n".join(lines)
