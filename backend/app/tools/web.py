"""Web tools: search (Tavily with DuckDuckGo fallback) and SSRF-safe page fetching."""

from __future__ import annotations

import asyncio
import io
import ipaddress
import socket
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings
from app.tools.base import Tool, ToolContext, ToolOutput

UA = "Mozilla/5.0 (compatible; AtlasAgent/1.0; +https://github.com/Aditya365x/atlas-agent)"
MAX_BYTES = 3_000_000
MAX_TEXT = 12_000


# ------------------------------- search ---------------------------------------------


async def _tavily(query: str, n: int, key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": query, "max_results": n, "search_depth": "basic"},
        )
        r.raise_for_status()
        return [
            {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")} for x in r.json().get("results", [])
        ]


async def _duckduckgo(query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}, follow_redirects=True) as c:
        r = await c.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}")
        r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for res in soup.select(".result")[: n * 2]:
        a = res.select_one("a.result__a")
        if not a or not a.get("href"):
            continue
        snip = res.select_one(".result__snippet")
        out.append({"title": a.get_text(" ", strip=True), "url": a["href"], "snippet": snip.get_text(" ", strip=True) if snip else ""})
        if len(out) >= n:
            break
    return out


async def web_search(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    query = str(args.get("query", "")).strip()
    n = max(1, min(int(args.get("max_results", 5) or 5), 8))
    if not query:
        return ToolOutput("Error: empty query", error=True)
    key = get_settings().tavily_api_key
    results = await (_tavily(query, n, key) if key else _duckduckgo(query, n))
    if not results:
        return ToolOutput(f"No results for: {query}", untrusted=True)
    lines = [f"[{i}] {r['title']}\nURL: {r['url']}\n{r['snippet']}" for i, r in enumerate(results, 1)]
    return ToolOutput("\n\n".join(lines), sources=[{"title": r["title"], "url": r["url"]} for r in results], untrusted=True)


# ------------------------------- fetch ----------------------------------------------


class UnsafeURL(ValueError):
    pass


async def assert_public_url(url: str) -> None:
    """Block SSRF: only http(s) to publicly routable addresses (no localhost, VPC, metadata IPs)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UnsafeURL("only http(s) URLs are allowed")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror as e:
        raise UnsafeURL(f"cannot resolve host {p.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL(f"blocked non-public address {ip}")


def html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    main = soup.find("article") or soup.find("main") or soup.body or soup
    lines = [ln.strip() for ln in main.get_text("\n").splitlines()]
    return title, "\n".join(ln for ln in lines if ln)


async def fetch_url(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    url = str(args.get("url", "")).strip()
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}, follow_redirects=False) as c:
            for _ in range(4):  # manual redirects so every hop is SSRF-checked
                await assert_public_url(url)
                async with c.stream("GET", url) as r:
                    if r.is_redirect:
                        url = urljoin(url, r.headers.get("location", ""))
                        continue
                    r.raise_for_status()
                    ctype = r.headers.get("content-type", "")
                    body = b""
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
                    break
            else:
                return ToolOutput("Error: too many redirects", error=True)
    except UnsafeURL as e:
        return ToolOutput(f"Error: refused to fetch URL ({e})", error=True)
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(body))
        title, text = url, "\n".join((pg.extract_text() or "") for pg in reader.pages[:30])
    elif "html" in ctype or not ctype:
        title, text = html_to_text(body.decode("utf-8", errors="replace"))
    else:
        title, text = url, body.decode("utf-8", errors="replace")
    text = text[:MAX_TEXT] + ("\n...[truncated]" if len(text) > MAX_TEXT else "")
    return ToolOutput(f"Title: {title}\nURL: {url}\n\n{text}", sources=[{"title": title or url, "url": url}], untrusted=True)


SEARCH_TOOL = Tool(
    name="web_search",
    description="Search the web for current information. Returns titles, URLs and snippets. Follow up with fetch_url to read a page.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Concise search query"},
            "max_results": {"type": "integer", "description": "1-8, default 5"},
        },
        "required": ["query"],
    },
    fn=web_search,
    timeout_s=30,
)

FETCH_TOOL = Tool(
    name="fetch_url",
    description="Fetch a public web page or PDF and return its readable text. Use to read sources found via web_search.",
    parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    fn=fetch_url,
    timeout_s=30,
)
