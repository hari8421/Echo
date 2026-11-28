"""Web research: search, read pages, write a sourced briefing."""

from __future__ import annotations

import base64
import html
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .config import Config
from .docs import DocStore
from .llm import LLM
from .util import slugify

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 Echo/0.1"
)
TIMEOUT = 20


class ResearchError(Exception):
    pass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass
class Page:
    url: str
    title: str
    text: str
    warning: str = ""


def _clean_url(href: str) -> str:
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and "uddg" in parse_qs(parsed.query):
        return unquote(parse_qs(parsed.query)["uddg"][0])
    return href


class _ResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: List[SearchResult] = []
        self._mode = None
        self._buffer: List[str] = []
        self._href = ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = attributes.get("class", "")
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self._flush()
            self._mode = "title"
            self._buffer = []
            self._href = attributes.get("href", "")
        elif "result__snippet" in classes:
            self._flush()
            self._mode = "snippet"
            self._buffer = []

    def handle_endtag(self, tag):
        if self._mode == "title" and tag == "a":
            self._flush()
        elif self._mode == "snippet" and tag in ("a", "div", "span"):
            self._flush()

    def handle_data(self, data):
        if self._mode:
            self._buffer.append(data)

    def _flush(self):
        if not self._mode:
            return
        text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        if self._mode == "title" and text:
            url = _clean_url(self._href)
            if url.startswith("http"):
                self.results.append(SearchResult(title=text, url=url))
        elif self._mode == "snippet" and text and self.results:
            if not self.results[-1].snippet:
                self.results[-1].snippet = text
        self._mode = None
        self._buffer = []

    def close(self):
        super().close()
        self._flush()


def parse_search_html(markup: str) -> List[SearchResult]:
    parser = _ResultParser()
    parser.feed(markup)
    parser.close()
    return parser.results


def _bing_url(href: str) -> str:
    """Undo Bing's ``/ck/a?u=a1<base64url>`` redirect wrapper."""
    href = html.unescape(href or "")
    parsed = urlparse(href)
    if "bing.com" in parsed.netloc:
        encoded = parse_qs(parsed.query).get("u", [""])[0]
        if encoded.startswith("a1"):
            try:
                value = encoded[2:]
                value += "=" * (-len(value) % 4)
                target = base64.urlsafe_b64decode(value).decode("utf-8")
                if target.startswith("http"):
                    return target
            except Exception:
                pass
    return href if href.startswith("http") else ""


class _BingParser(HTMLParser):
    """Read the result blocks Bing serves at ``/search``."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: List[SearchResult] = []
        self._current: Optional[SearchResult] = None
        self._mode = ""
        self._buffer: List[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = attributes.get("class", "")
        if tag == "li" and "b_algo" in classes:
            self._current = SearchResult(title="", url="")
            self._mode = ""
            self._buffer = []
            return
        if self._current is None:
            return
        if tag == "h2":
            self._flush()
            self._mode = "title"
        elif tag == "a" and self._mode == "title" and not self._current.url:
            self._current.url = _bing_url(attributes.get("href", ""))
        elif tag == "p":
            self._flush()
            self._mode = "snippet"

    def handle_endtag(self, tag):
        if self._current is None:
            return
        if (tag == "h2" and self._mode == "title") or (tag == "p" and self._mode == "snippet"):
            self._flush()
        elif tag == "li":
            self._flush()
            if self._current.url and self._current.title:
                self.results.append(self._current)
            self._current = None
            self._mode = ""

    def handle_data(self, data):
        if self._mode:
            self._buffer.append(data)

    def _flush(self):
        if not self._mode or self._current is None:
            return
        text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        if text:
            if self._mode == "title" and not self._current.title:
                self._current.title = text
            elif self._mode == "snippet" and not self._current.snippet:
                self._current.snippet = text
        self._mode = ""
        self._buffer = []


def parse_bing_html(markup: str) -> List[SearchResult]:
    parser = _BingParser()
    parser.feed(markup)
    parser.close()
    return parser.results


def _fetch(url: str, data: bytes = None) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def web_search(query: str, limit: int = 5) -> List[SearchResult]:
    """Search the web, trying providers in order until one returns results."""
    limit = max(1, min(int(limit or 5), 20))
    failures: List[str] = []

    try:
        markup = _fetch(
            "https://www.bing.com/search",
            urllib.parse.urlencode({"q": query, "count": max(10, limit * 2)}).encode("utf-8"),
        )
        results = parse_bing_html(markup)
        if results:
            return results[:limit]
        failures.append("Bing returned no results")
    except (OSError, ValueError) as exc:
        failures.append("Bing: %s" % exc)

    # DuckDuckGo is kept as a fallback: it works from some networks/regions.
    try:
        markup = _fetch(
            "https://html.duckduckgo.com/html/",
            urllib.parse.urlencode({"q": query}).encode("utf-8"),
        )
        results = parse_search_html(markup)
        if results:
            return results[:limit]
        failures.append("DuckDuckGo returned no results")
    except (OSError, ValueError) as exc:
        failures.append("DuckDuckGo: %s" % exc)

    raise ResearchError(
        "Web search found nothing for %r (%s). Try a shorter query or check your connection."
        % (query[:80], "; ".join(failures))
    )


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "form"}
    BLOCK = {
        "p", "div", "br", "li", "tr", "section", "article", "h1", "h2", "h3",
        "h4", "h5", "h6", "blockquote", "pre", "table", "ul", "ol",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.parts.append(data)


def extract_text(markup: str) -> tuple:
    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    text = "".join(extractor.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return extractor.title.strip(), text.strip()


def fetch_page(url: str, max_chars: int = 20000) -> Page:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(2_000_000)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ResearchError("Could not read %s (%s)" % (url, exc)) from exc
    if "html" not in content_type and "text" not in content_type:
        raise ResearchError("%s is not a readable web page (%s)" % (url, content_type))
    markup = raw.decode("utf-8", errors="replace")
    title, text = extract_text(markup)
    if not text:
        raise ResearchError("No readable text at %s" % url)
    return Page(url=url, title=title or url, text=text[:max_chars])


SYSTEM = (
    "You are Echo's research assistant. Write a compact, practical briefing "
    "using only the supplied source excerpts. Use markdown with a short "
    "overview, 3-6 key findings as bullets, and a caveats section noting "
    "conflicts or missing evidence. Attribute claims to sources inline like "
    "[S1]. Do not invent facts, statistics, quotes or URLs. Aim for 350-550 "
    "words. Do not add a sources section; it is appended automatically."
)


def research(
    cfg: Config,
    store: DocStore,
    llm: LLM,
    topic: str,
    max_sources: int = 4,
) -> tuple:
    topic = topic.strip()
    if not topic:
        raise ResearchError("Give me a topic to research.")
    results = web_search(topic, limit=max_sources * 2)
    pages: List[Page] = []
    failures: List[str] = []
    for result in results:
        if len(pages) >= max_sources:
            break
        try:
            page = fetch_page(result.url)
        except ResearchError as exc:
            failures.append(str(exc))
            continue
        if len(page.text) < 200:
            continue
        page.title = result.title or page.title
        pages.append(page)
    if not pages:
        raise ResearchError(
            "Search found results but none could be read as a page. "
            + (failures[0] if failures else "")
        )

    source_blocks = []
    for index, page in enumerate(pages, start=1):
        source_blocks.append(
            "[S%d] %s\nURL: %s\n%s" % (index, page.title, page.url, page.text[:5000])
        )
    user = "Topic: %s\n\nSources:\n\n%s" % (topic, "\n\n".join(source_blocks))
    response = llm.chat(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ]
    )
    body = (response.get("content") or "").strip()
    if not body:
        raise ResearchError("The model returned an empty briefing.")
    sources = "\n".join(
        "- [%s](%s)" % (page.title.replace("[", "(").replace("]", ")"), page.url)
        for page in pages
    )
    report = body + "\n\n## Sources\n\n" + sources + "\n"
    doc = store.create(
        "Research: " + topic[:80],
        report,
        tags=["research", slugify(topic)[:40]],
    )
    return doc, pages, failures
