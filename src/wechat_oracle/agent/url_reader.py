"""Fetch and extract readable text from public HTTP(S) pages."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from .tools import ToolError, truncate_for_llm


class _ReadableHTMLParser(HTMLParser):
    """Small dependency-free extractor for article-like HTML."""

    _BLOCK_TAGS = {
        "article", "section", "p", "br", "div", "li", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str = ""
        self._in_title = False
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title = (self.title + " " + text).strip()
        else:
            self._chunks.append(text)

    def body_text(self) -> str:
        raw = " ".join(self._chunks)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\s*\n\s*", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def read_public_url(url: str, *, max_chars: int = 12000) -> str:
    """Fetch a public HTTP(S) URL and return title + readable text."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolError("url must be an absolute http(s) URL")

    try:
        resp = httpx.get(
            url,
            follow_redirects=True,
            timeout=25.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolError(f"failed to fetch url: {exc}") from exc

    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        snippet = resp.text[:1000] if resp.text else ""
        if snippet:
            return truncate_for_llm(
                f"URL: {str(resp.url)}\ncontent-type: {content_type or '(unknown)'}\n\n"
                f"{unescape(snippet)}",
                limit=max_chars,
            )
        return f"URL: {str(resp.url)}\ncontent-type: {content_type or '(unknown)'}\n(empty body)"

    parser = _ReadableHTMLParser()
    parser.feed(resp.text)
    title = unescape(parser.title).strip()
    body = unescape(parser.body_text()).strip()
    if not body:
        return f"URL: {str(resp.url)}\nTitle: {title or '(none)'}\n(no readable text extracted)"

    header = f"URL: {str(resp.url)}"
    if title:
        header += f"\nTitle: {title}"
    return truncate_for_llm(f"{header}\n\n{body}", limit=max_chars)
