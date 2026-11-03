"""Turn HTML files and web pages into readable chapters.

The extractor is deliberately lightweight (BeautifulSoup only): it strips
site chrome, scores likely article containers, keeps headings/paragraphs and
drops navigation- or link-heavy blocks. It runs fine on desktop and mobile and
is used for both ``.html`` imports and links pasted into the library.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from soundbook.core.document import Book, Chapter, clean_text

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 20.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Soundbook/0.1"
)

_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "iframe",
    "svg",
    "button",
    "input",
    "select",
    "dialog",
    "table",
)
_BLOCK_TAGS = (
    "h1",
    "h2",
    "h3",
    "h4",
    "p",
    "li",
    "blockquote",
    "pre",
    "figcaption",
)
_HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
_CHROME = re.compile(
    r"(?:^|[-_ ])(?:nav|navbox|navigation|menu|sidebar|side-bar|footer|header|"
    r"share|sharing|social|comment|comments|related|toc|breadcrumb|advert|"
    r"advertisement|promo|newsletter|subscribe|cookie|modal|popup|banner|"
    r"widget|infobox|editsection|catlinks|printfooter|noprint|story_info|"
    r"series_link)(?:[-_ ]|$)",
    re.IGNORECASE,
)
_CHROME_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search"}
_CITATION = re.compile(r"\[\s*\d+\s*\]")
_META_HEADING = re.compile(
    r"(?:^|[-_ ])(?:author|authors|byline|dateline|date|published|publish|"
    r"genre|genres|subtitle|dek|kicker|word[-_ ]?count|reading[-_ ]?time)"
    r"(?:[-_ ]|$)",
    re.IGNORECASE,
)
_META_ITEMPROPS = {"author", "datePublished", "genre", "wordCount", "publisher"}


@dataclass
class _Block:
    tag: str
    text: str


def normalize_url(url: str) -> str:
    """Accept ``example.com/page`` as well as a full http(s) URL."""
    url = url.strip()
    if not url:
        raise ValueError("paste a link first")
    if "://" not in url:
        url = f"https://{url}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("enter a full http:// or https:// link")
    return parsed.geturl()


def fetch_page(url: str, timeout: float = TIMEOUT) -> tuple[str, str]:
    """Download an HTML page, following redirects; returns (final_url, text)."""
    parsed = urllib.parse.urlparse(normalize_url(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("enter a full http:// or https:// link")

    request = urllib.request.Request(
        parsed.geturl(),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError(
                    f"link is {content_type or 'not HTML'}, not a web page"
                )
            raw = response.read(MAX_BYTES + 1)
            final_url = response.geturl()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise ValueError(f"the site returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"could not reach the site: {exc.reason}") from exc

    if len(raw) > MAX_BYTES:
        raise ValueError("page is too large to import")

    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return final_url, text


def parse_url(url: str, timeout: float = TIMEOUT) -> tuple[Book, str]:
    """Fetch a link and return (book, final source URL)."""
    final_url, raw = fetch_page(url, timeout=timeout)
    fallback = urllib.parse.urlparse(final_url).netloc or "Web page"
    book = html_to_book(raw, fallback_title=fallback)
    return book, final_url


def parse_html_file(path: Path) -> Book:
    raw = path.read_bytes()
    return html_to_book(raw, fallback_title=path.stem)


def html_to_book(raw: str | bytes, fallback_title: str = "") -> Book:
    """Extract the main article from an HTML document."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()
    author = _find_author(soup)
    _strip_chrome(soup)

    container = _main_container(soup)
    title = (
        _node_text(container.find("h1"))
        or _meta(soup, "og:title", "twitter:title")
        or _node_text(soup.find("h1"))
        or _node_text(soup.find("title"))
        or fallback_title
    )

    chapters = _chapters_from_blocks(_collect_blocks(container), title)

    if not chapters:
        text = clean_text(container.get_text("\n"))
        if len(text) >= 20:
            chapters = [Chapter(title or fallback_title, text)]

    if not chapters:
        raise ValueError("no readable text found on the page")

    return Book(title=title or fallback_title, author=author, chapters=chapters)


# extraction ----------------------------------------------------------------


def _node_text(node) -> str:
    if node is None:
        return ""
    # get_text() without a separator preserves text-node spacing, so a drop
    # cap written as <span>T</span>he becomes "The", not "T he".
    text = " ".join(node.get_text().split())
    text = _CITATION.sub("", text)
    text = re.sub(r"\s+([,.;:!?%])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]])", r"\1", text)
    return " ".join(text.split())


def _class_marker(node) -> str:
    classes = node.get("class") or []
    if isinstance(classes, str):
        classes = [classes]
    return " ".join([*classes, node.get("id") or ""])


def _strip_chrome(soup) -> None:
    """Remove navigation-like blocks that survived the tag-level clean-up."""
    for node in soup.find_all(
        ["div", "section", "p", "ul", "ol", "span", "a"]
    ):
        if getattr(node, "decomposed", False):
            continue
        role = (node.get("role") or "").lower()
        if role in _CHROME_ROLES or node.get("aria-hidden") == "true":
            node.decompose()
            continue
        marker = _class_marker(node)
        if marker and _CHROME.search(marker):
            node.decompose()


def _meta(soup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name})
        if tag is None:
            tag = soup.find("meta", attrs={"name": name})
        if tag is not None:
            content = " ".join((tag.get("content") or "").split())
            if content:
                return content
    return ""


def _find_author(soup) -> str:
    author = _meta(soup, "author", "article:author", "og:article:author")
    if author and not author.startswith(("http://", "https://")):
        return author

    for attrs in ({"itemprop": "author"}, {"itemprop": "creator"}, {"rel": "author"}):
        node = soup.find(attrs=attrs)
        if node is None:
            continue
        text = _node_text(node)
        if text and len(text) <= 120 and not text.startswith(("http://", "https://")):
            return text
    return ""


def _link_share(node) -> float:
    text = _node_text(node)
    if not text:
        return 1.0
    linked = sum(len(_node_text(link)) for link in node.find_all("a"))
    return min(1.0, linked / len(text))


def _content_score(node) -> int:
    """Text length with link-heavy navigation discounted."""
    text = _node_text(node)
    if not text:
        return 0
    return int(len(text) * (1.0 - min(0.9, _link_share(node))))


def _main_container(soup):
    body = soup.body or soup
    body_score = _content_score(body)
    if body_score < 200:
        return body

    candidates = []
    for selector in ("article", "main", "[role=main]"):
        candidates.extend(soup.select(selector))
    candidates.extend(soup.find_all(["div", "section"]))

    best = None
    best_score = 0
    for node in candidates:
        score = _content_score(node)
        if score > best_score:
            best, best_score = node, score

    # A weak "article" (a card, a teaser) is worse than the whole body.
    if best is None or best_score < body_score * 0.5:
        return body
    return best


def _is_meta_heading(node) -> bool:
    itemprops = node.get("itemprop") or []
    if isinstance(itemprops, str):
        itemprops = [itemprops]
    if any(prop in _META_ITEMPROPS for prop in itemprops):
        return True
    marker = _class_marker(node)
    return bool(marker and _META_HEADING.search(marker))


def _collect_blocks(container) -> list[_Block]:
    blocks: list[_Block] = []
    selected: set[int] = set()

    for node in container.find_all(_BLOCK_TAGS):
        if node.name == "h1":  # the article title is stored as the book title
            continue
        if node.name in {"h2", "h3", "h4"} and _is_meta_heading(node):
            continue
        if any(id(parent) in selected for parent in node.parents):
            continue
        selected.add(id(node))

        text = _node_text(node)
        if len(text) < 2:
            continue
        if node.name not in {"h2", "h3", "h4"} and _link_share(node) > 0.8:
            continue
        blocks.append(_Block(node.name, text))

    return blocks


def _chapters_from_blocks(blocks: list[_Block], title: str) -> list[Chapter]:
    chapters: list[Chapter] = []
    chapter_title = title
    body: list[str] = []
    has_body = False

    def flush() -> None:
        nonlocal body, has_body
        text = clean_text("\n\n".join(body))
        if has_body and len(text) >= 20:
            chapters.append(Chapter(chapter_title or title, text))
        body = []
        has_body = False

    for block in blocks:
        if block.tag == "h2":
            if has_body:
                flush()
            chapter_title = block.text
            body = [block.text]
            continue
        body.append(block.text)
        has_body = True

    flush()

    if len(chapters) <= 1:
        text = clean_text("\n\n".join(block.text for block in blocks))
        if len(text) >= 20:
            chapter_title = title or (blocks[0].text if blocks else "Web page")
            return [Chapter(chapter_title, text)]
    return chapters


def looks_like_html(path: Path) -> bool:
    return path.suffix.lower() in _HTML_SUFFIXES