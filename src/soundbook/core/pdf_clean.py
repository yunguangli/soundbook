"""Layout clean-up helpers for text extracted from PDFs.

The same rules are used by the in-app ``parse_pdf`` fallback and by the
stand-alone ``scripts/pdf2text.py`` preprocessor, so both paths remove the
same running headers, footers and table noise.

Nothing in this module imports PyMuPDF, which keeps it testable on the web
build as well.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence

# Fraction of the page height treated as top/bottom margin.
MARGIN_RATIO = 0.06
# A line repeated on this share of pages is furniture (header/footer).
REPEAT_RATIO = 0.5
# Compare this many characters when looking for repeated furniture. Wrapped
# footers often differ only in their final punctuation.
SIGNATURE_CHARS = 120

_PAGE_NUMBER = re.compile(
    r"^(?:page\s*)?\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?$",
    re.IGNORECASE,
)
_SOURCE_LINE = re.compile(r"^(?:source|sources|chart source)\s*:", re.IGNORECASE)
_HYPHEN_BREAK = re.compile(r"([^\W\d_])-[ \t]*\n+[ \t]*([^\W\d_])")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_PICTURE_TEXT = re.compile(
    r"<!--\s*Start of picture text\s*-->.*?<!--\s*End of picture text\s*-->",
    re.DOTALL,
)
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_REF_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")


def normalize_line(line: str) -> str:
    """Case-folded, whitespace-collapsed form used for comparisons."""
    return " ".join(line.split()).casefold()


def signature(line: str) -> str:
    """Stable prefix of a line for repeated-header detection."""
    normalized = normalize_line(line)
    return normalized[:SIGNATURE_CHARS].rstrip(" .,;:-–—")


def is_furniture(line: str) -> bool:
    """True for page numbers and table/chart source captions."""
    stripped = line.strip()
    if _PAGE_NUMBER.fullmatch(stripped):
        return True
    return bool(_SOURCE_LINE.match(stripped)) and len(stripped) < 120


def repeated_lines(
    pages: Sequence[Iterable[str]],
    ratio: float = REPEAT_RATIO,
) -> set[str]:
    """Signatures of lines that occur on at least ``ratio`` of the pages."""
    total = len(pages)
    if total < 2:
        return set()

    threshold = max(2, math.ceil(total * ratio))
    counts: dict[str, int] = {}
    for lines in pages:
        seen = {sig for line in lines if (sig := signature(line))}
        for sig in seen:
            counts[sig] = counts.get(sig, 0) + 1
    return {sig for sig, count in counts.items() if count >= threshold}


def is_repeated(line: str, junk: Iterable[str]) -> bool:
    """Whether ``line`` matches one of the signatures in ``junk``."""
    return signature(line) in junk


def repair_hyphenation(text: str) -> str:
    """Join words split across a line break: ``exam-\\nple`` -> ``example``."""
    return _HYPHEN_BREAK.sub(r"\1\2", text)


def strip_markup(text: str) -> str:
    """Remove the Markdown/HTML noise that should never be spoken."""
    text = _PICTURE_TEXT.sub(" ", text)
    text = _HTML_COMMENT.sub(" ", text)
    text = _MD_IMAGE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_REF_LINK.sub(r"\1", text)
    text = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def _is_numeric_token(token: str) -> bool:
    cleaned = token.strip("()[]{}<>.,;:%$€£¥+-–—/\\'\"\u00a0")
    if not cleaned:
        return False
    return bool(re.fullmatch(r"[\d,.\s]+", cleaned))


def is_numeric_text(text: str) -> bool:
    """True for short cells that are mostly numbers (``60,922``, ``FY2024``)."""
    stripped = " ".join(text.split())
    if not stripped or len(stripped) > 40:
        return False

    tokens = stripped.split()
    numeric = sum(1 for token in tokens if _is_numeric_token(token))
    if numeric and numeric / len(tokens) >= 0.5:
        return True

    digits = sum(char.isdigit() for char in stripped)
    letters = sum(char.isalpha() for char in stripped)
    if not digits:
        return False
    if not letters:
        return True
    return len(stripped) <= 20 and digits / (digits + letters) >= 0.3


def is_table_line(
    line: str,
    min_numeric: int = 3,
    numeric_share: float = 0.4,
    max_chars: int = 160,
) -> bool:
    """True for a line that is mostly numbers, such as a flattened table row."""
    stripped = " ".join(line.split())
    if len(stripped) > max_chars:
        return False

    tokens = stripped.split()
    if len(tokens) < min_numeric:
        return False
    if len(tokens) > 3 and stripped[-1] in ".!?":
        return False  # prose, not a table row
    numeric = sum(1 for token in tokens if any(char.isdigit() for char in token))
    return numeric >= min_numeric and numeric / len(tokens) >= numeric_share


# Block tuples are (x0, y0, x1, y1, text).
Block = tuple[float, float, float, float, str]


def group_rows(blocks: Sequence[Block], tolerance: float = 4.0) -> list[list[Block]]:
    """Group blocks that share the same visual line, ordered top-to-bottom."""
    ordered = sorted(blocks, key=lambda block: ((block[1] + block[3]) / 2, block[0]))
    rows: list[tuple[float, list[Block]]] = []

    for block in ordered:
        center = (block[1] + block[3]) / 2
        if rows and abs(center - rows[-1][0]) <= tolerance:
            rows[-1][1].append(block)
        else:
            rows.append((center, [block]))

    return [sorted(row, key=lambda block: block[0]) for _, row in rows]


def is_table_row(
    row: Sequence[Block],
    min_blocks: int = 3,
    numeric_share: float = 0.5,
) -> bool:
    """A row of three or more mostly-numeric cells is a flattened table row."""
    if len(row) < min_blocks:
        return False
    numeric = sum(1 for block in row if is_numeric_text(block[4]))
    return numeric >= max(2, math.ceil(len(row) * numeric_share))


def drop_table_rows(blocks: Sequence[Block]) -> list[Block]:
    """Drop rows that look like table cells, keeping normal paragraphs."""
    kept: list[Block] = []
    for row in group_rows(blocks):
        if not is_table_row(row):
            kept.extend(row)
    return kept


def drop_numeric_lines(lines: Sequence[str], min_numeric: int = 5) -> list[str]:
    """Drop bare numbers when a page is mostly a flattened table.

    Plain-text extractors such as ``pypdf`` lose coordinates, so a page-wide
    heuristic is the best available signal there.
    """
    numeric = sum(1 for line in lines if is_numeric_text(line))
    if numeric < min_numeric:
        return list(lines)
    return [line for line in lines if not is_numeric_text(line)]


def clean_markdown_page(
    text: str,
    junk: Iterable[str],
    keep_tables: bool = False,
) -> str:
    """Clean one page of ``pymupdf4llm`` Markdown output."""
    text = _PICTURE_TEXT.sub("", text)
    text = _HTML_COMMENT.sub("", text)
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()

        if not stripped:
            kept.append("")
            continue
        if stripped.startswith("|"):
            if keep_tables:
                kept.append(line.rstrip())
            continue
        if is_furniture(stripped) or is_repeated(stripped, junk):
            continue
        if not keep_tables and is_table_line(stripped):
            continue
        kept.append(line.rstrip())

    cleaned = repair_hyphenation("\n".join(kept))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def margin_furniture(
    pages: Iterable[Sequence[Block]],
    page_heights: Iterable[float],
    margin: float = MARGIN_RATIO,
) -> set[str]:
    """Signatures of blocks sitting in the top or bottom margin band."""
    junk: set[str] = set()
    for blocks, height in zip(pages, page_heights):
        low, high = height * margin, height * (1 - margin)
        for block in blocks:
            center = (block[1] + block[3]) / 2
            if center < low or center > high:
                for line in block[4].splitlines():
                    if sig := signature(line):
                        junk.add(sig)
    return junk
