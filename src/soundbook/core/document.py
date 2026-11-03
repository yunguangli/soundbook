"""Parse EPUB, PDF, Markdown and plain text into chapters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from soundbook.core.pdf_clean import (
    MARGIN_RATIO,
    REPEAT_RATIO,
    Block,
    drop_numeric_lines,
    drop_table_rows,
    is_furniture,
    is_repeated,
    is_table_line,
    repeated_lines,
    repair_hyphenation,
    signature,
    strip_markup,
)

SUPPORTED_EXTENSIONS = {
    ".epub",
    ".pdf",
    ".txt",
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".xhtml",
}

_TXT_HEADING = re.compile(
    r"^[ \t]*(chapter|part|book)\s+([0-9]+|[ivxlcdm]+|one|two|three|four|five|"
    r"six|seven|eight|nine|ten)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)

_MD_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_MD_SETEXT = re.compile(r"^(=+|-{2,})[ \t]*$")
_MD_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)


@dataclass
class Chapter:
    title: str
    text: str


@dataclass
class Book:
    title: str
    author: str
    chapters: list[Chapter] = field(default_factory=list)


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")  # pragma: no cover


def _metadata(book, name: str) -> str:
    try:
        values = book.get_metadata("DC", name)
    except Exception:
        return ""
    if not values:
        return ""
    value = values[0][0]
    return str(value).strip() if value else ""


def _heading(soup, fallback: str) -> str:
    for tag in ("h1", "h2", "h3", "title"):
        node = soup.find(tag)
        if node:
            text = " ".join(node.get_text(" ", strip=True).split())
            if text:
                return text[:200]
    return fallback


def parse_epub(path: Path) -> Book:
    import ebooklib
    from bs4 import BeautifulSoup
    from ebooklib import epub

    epub_book = epub.read_epub(str(path))

    items = []
    for idref, _linear in epub_book.spine:
        item = epub_book.get_item_with_id(idref)
        if item is not None and item.get_type() == ebooklib.ITEM_DOCUMENT:
            items.append(item)
    if not items:
        items = list(epub_book.get_items_of_type(ebooklib.ITEM_DOCUMENT))

    chapters: list[Chapter] = []
    for index, item in enumerate(items, start=1):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for tag in soup(["script", "style", "nav"]):
            tag.decompose()
        text = clean_text(soup.get_text("\n"))
        if len(text) < 50:
            continue
        chapters.append(
            Chapter(title=_heading(soup, f"Chapter {index}"), text=text)
        )

    return Book(
        title=_metadata(epub_book, "title") or path.stem,
        author=_metadata(epub_book, "creator"),
        chapters=chapters,
    )


# PDF -----------------------------------------------------------------------


def _pdf_blocks(page) -> tuple[list[Block], list[str]]:
    """Text blocks outside the margin bands, plus every line on the page.

    The unfiltered lines are used to spot running headers and footers across
    pages; the blocks are what actually gets read.
    """
    height = page.rect.height
    low, high = height * MARGIN_RATIO, height * (1 - MARGIN_RATIO)
    blocks: list[Block] = []
    lines: list[str] = []

    for raw in page.get_text("blocks"):
        x0, y0, x1, y1, text = raw[:5]
        if len(raw) > 6 and raw[6] != 0:  # image block
            continue
        if not text or not text.strip():
            continue
        lines.extend(text.splitlines())
        center = (y0 + y1) / 2
        if center < low or center > high:
            continue
        blocks.append((x0, y0, x1, y1, text))

    return blocks, lines


def _block_text(block: Block, junk: set[str]) -> str:
    kept: list[str] = []
    for line in block[4].splitlines():
        stripped = " ".join(line.split())
        if not stripped:
            continue
        if is_furniture(stripped) or is_repeated(stripped, junk):
            continue
        if is_table_line(stripped):
            continue
        kept.append(stripped)
    if not kept:
        return ""
    if is_table_line(" ".join(kept)):
        return ""
    return "\n".join(kept)


def _is_two_column(blocks: list[Block], width: float) -> bool:
    if len(blocks) < 6:
        return False

    wide = sum(1 for block in blocks if block[2] - block[0] > width * 0.55)
    if wide >= max(2, len(blocks) * 0.25):
        return False

    middle = width / 2
    left = sum(1 for block in blocks if (block[0] + block[2]) / 2 < middle)
    return left >= 2 and len(blocks) - left >= 2


def _ordered_blocks(blocks: list[Block], width: float) -> list[Block]:
    if _is_two_column(blocks, width):
        middle = width / 2
        left = sorted(
            (b for b in blocks if (b[0] + b[2]) / 2 < middle),
            key=lambda b: (b[1], b[0]),
        )
        right = sorted(
            (b for b in blocks if (b[0] + b[2]) / 2 >= middle),
            key=lambda b: (b[1], b[0]),
        )
        return left + right
    return drop_table_rows(blocks)


def _page_text(page, junk: set[str]) -> str:
    blocks, _lines = _pdf_blocks(page)
    if not blocks:
        return ""

    ordered = _ordered_blocks(blocks, page.rect.width)
    paragraphs = [text for b in ordered if (text := _block_text(b, junk))]
    text = "\n\n".join(paragraphs)
    return clean_text(repair_hyphenation(text))


def _toc_entries(doc) -> list[tuple[int, int, str]]:
    """TOC entries at the shallowest level that actually spans the document."""
    raw = doc.get_toc(simple=True) or []
    entries = [
        (int(level), int(page), " ".join(title.split())[:200])
        for level, title, page in raw
        if title and str(title).strip()
    ]
    if not entries:
        return []

    by_level: dict[int, list[tuple[int, int, str]]] = {}
    for entry in entries:
        by_level.setdefault(entry[0], []).append(entry)

    for level in sorted(by_level):
        candidates = sorted(by_level[level], key=lambda e: e[1])
        pages = {page for _level, page, _title in candidates}
        if len(candidates) >= 2 and len(pages) >= 2:
            return candidates
    return []


def _toc_chapters(doc, page_texts: list[str]) -> list[Chapter]:
    entries = _toc_entries(doc)
    if not entries:
        return []

    total = len(page_texts)
    entries = [
        (page, title)
        for _level, page, title in entries
        if 1 <= page <= total
    ]
    if len(entries) < 2:
        return []

    chapters: list[Chapter] = []
    first_page = entries[0][0]
    if first_page > 1:
        front = "\n\n".join(page_texts[: first_page - 1])
        if len(front) >= 20:
            chapters.append(Chapter("Preface", front))

    for index, (page, title) in enumerate(entries):
        end = entries[index + 1][0] if index + 1 < len(entries) else total + 1
        text = "\n\n".join(page_texts[page - 1 : end - 1])
        if len(text) >= 20:
            chapters.append(Chapter(title, text))

    return chapters if len(chapters) >= 2 else []


def _parse_pdf_native(path: Path) -> Book:
    import pymupdf

    doc = pymupdf.open(str(path))
    try:
        page_blocks = [_pdf_blocks(page) for page in doc]
        junk = repeated_lines(
            [lines for _blocks, lines in page_blocks], REPEAT_RATIO
        )
        junk.update(
            signature(line)
            for _blocks, lines in page_blocks
            for line in lines
            if is_furniture(line)
        )

        page_texts = [_page_text(page, junk) for page in doc]
        chapters = _toc_chapters(doc, page_texts)
        if not chapters:
            chapters = [
                Chapter(f"Page {index}", text)
                for index, text in enumerate(page_texts, start=1)
                if len(text) >= 20
            ]

        title = ((doc.metadata or {}).get("title") or "").strip() or path.stem
        return Book(title=title, author="", chapters=chapters)
    finally:
        doc.close()


def _parse_pdf_pypdf(path: Path) -> Book:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    junk = repeated_lines([page.splitlines() for page in pages], REPEAT_RATIO)

    cleaned: list[str] = []
    for text in pages:
        lines = [
            " ".join(line.split())
            for line in text.splitlines()
            if line.strip()
            and not is_furniture(line)
            and not is_repeated(line, junk)
        ]
        cleaned.append(clean_text(repair_hyphenation("\n".join(drop_numeric_lines(lines)))))

    chapters = [
        Chapter(f"Page {index}", text)
        for index, text in enumerate(cleaned, start=1)
        if len(text) >= 20
    ]
    return Book(title=path.stem, author="", chapters=chapters)


def parse_pdf(path: Path) -> Book:
    """PyMuPDF with layout clean-up when available, pypdf on the web.

    The in-app parser is a best-effort fallback. For long or complex PDFs run
    ``scripts/pdf2text.py`` first and import the resulting Markdown.
    """
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        return _parse_pdf_pypdf(path)
    return _parse_pdf_native(path)


# Markdown ------------------------------------------------------------------


def _front_matter(text: str) -> tuple[str, dict[str, str]]:
    match = _MD_FRONT_MATTER.match(text)
    if not match:
        return text, {}

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        cleaned = value.strip().strip("\"'")
        if cleaned:
            meta[key.strip().lower()] = cleaned
    return text[match.end() :], meta


def _md_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    if stripped.startswith("```") or stripped.startswith("~~~"):
        return ""
    if re.fullmatch(r"[-*_](\s*[-*_]){2,}", stripped):  # horizontal rule
        return ""

    if stripped.startswith("|"):
        cells = [
            cell.strip()
            for cell in stripped.strip("|").split("|")
            if cell.strip() and not re.fullmatch(r":?-{3,}:?", cell.strip())
        ]
        return strip_markup(", ".join(cells))

    stripped = re.sub(r"^>\s?", "", stripped)  # block quote
    stripped = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", stripped)  # list marker
    return strip_markup(stripped)


def _md_heading(line: str, next_line: str) -> tuple[str, int] | None:
    match = _MD_HEADING.match(line)
    if match:
        title = strip_markup(match.group(2)).strip()
        return (title, len(match.group(1))) if title else None

    underline = _MD_SETEXT.match(next_line.strip())
    if underline and line.strip() and not _MD_HEADING.match(line):
        title = strip_markup(line).strip()
        return (title, 1 if underline.group(1).startswith("=") else 2) if title else None

    return None


def _split_md_chapters(text: str, fallback: str) -> list[Chapter]:
    lines = text.splitlines()
    chapters: list[Chapter] = []

    title = fallback
    body: list[str] = []
    has_body = False

    def flush() -> None:
        nonlocal body, has_body
        joined = clean_text("\n".join(body))
        if has_body and len(joined) >= 20:
            chapters.append(Chapter(title, joined))
        body = []
        has_body = False

    index = 0
    while index < len(lines):
        line = lines[index]
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        heading = _md_heading(line, next_line)
        if heading is not None:
            flush()
            title, _level = heading
            body = [title]
            index += 2 if _MD_SETEXT.match(next_line.strip()) else 1
            continue

        cleaned = _md_line(line)
        if cleaned:
            has_body = True
        body.append(cleaned)
        index += 1

    flush()
    return chapters


def parse_md(path: Path) -> Book:
    text, meta = _front_matter(_read_text(path))
    chapters = _split_md_chapters(text, path.stem)

    if not chapters:
        body = clean_text(_md_line_block(text))
        if len(body) >= 20:
            chapters = [Chapter(meta.get("title") or path.stem, body)]

    return Book(
        title=meta.get("title") or path.stem,
        author=meta.get("author", ""),
        chapters=chapters,
    )


def _md_line_block(text: str) -> str:
    return "\n".join(line for line in (_md_line(raw) for raw in text.splitlines()) if line)


# Plain text ----------------------------------------------------------------


def _split_txt_chapters(text: str) -> list[Chapter]:
    matches = list(_TXT_HEADING.finditer(text))
    if len(matches) < 2:
        return []

    chapters: list[Chapter] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = clean_text(text[start:end])
        if len(body) < 20:
            continue
        title = " ".join(match.group(0).split())[:200]
        chapters.append(Chapter(title=title, text=body))
    return chapters


def parse_txt(path: Path) -> Book:
    text = clean_text(_read_text(path))
    chapters = _split_txt_chapters(text)
    if not chapters and len(text) >= 20:
        chapters = [Chapter(title=path.stem, text=text)]
    return Book(title=path.stem, author="", chapters=chapters)


def parse_book(path: Path | str) -> Book:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".epub":
        book = parse_epub(path)
    elif suffix == ".pdf":
        book = parse_pdf(path)
    elif suffix in {".md", ".markdown"}:
        book = parse_md(path)
    elif suffix in {".html", ".htm", ".xhtml"}:
        from soundbook.core.webpage import parse_html_file

        book = parse_html_file(path)
    elif suffix == ".txt":
        book = parse_txt(path)
    else:
        raise ValueError(f"unsupported format: {suffix or path.name}")

    if not book.chapters:
        raise ValueError(f"no readable text found in {path.name}")
    return book
