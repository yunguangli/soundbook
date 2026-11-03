"""Split chapter text into sentences and pages."""

from __future__ import annotations

from soundbook.core.document import clean_text

TERMINATORS = ".!?…"
CLOSERS = "\"'”’)"

ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs", "etc",
    "e.g", "i.e", "no", "fig", "vol", "ch", "p", "pp",
}

DEFAULT_PAGE_CHARS = 360


def _paragraphs(text: str):
    for block in clean_text(text).split("\n\n"):
        collapsed = " ".join(block.split())
        if collapsed:
            yield collapsed


def _is_abbreviation(sentence_so_far: str) -> bool:
    words = sentence_so_far.split()
    if not words:
        return False
    last = words[-1].lower().strip(".,;:!?\"'()[]")
    if last in ABBREVIATIONS:
        return True
    # Initials: "U.S", "J", "A.B"
    compact = last.replace(".", "")
    return bool(compact) and len(compact) <= 3 and last == last.upper()


def split_sentences(text: str) -> list[str]:
    """Split text into sentences, keeping punctuation attached."""
    sentences: list[str] = []

    for paragraph in _paragraphs(text):
        start = 0
        index = 0
        length = len(paragraph)

        while index < length:
            char = paragraph[index]
            if char not in TERMINATORS:
                index += 1
                continue

            end = index + 1
            while end < length and paragraph[end] in TERMINATORS:
                end += 1
            while end < length and paragraph[end] in CLOSERS:
                end += 1

            if end < length and not paragraph[end].isspace():
                index = end
                continue

            candidate = paragraph[start:end].strip()
            if candidate and not _is_abbreviation(candidate):
                sentences.append(candidate)
                start = end
            index = end

        tail = paragraph[start:].strip()
        if tail:
            sentences.append(tail)

    return [sentence for sentence in sentences if sentence]


def split_long(text: str, max_chars: int) -> list[str]:
    """Split an overlong sentence on word boundaries."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for word in text.split():
        if current and len(current) + len(word) + 1 > max_chars:
            chunks.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        chunks.append(current)
    return chunks


def paginate(
    sentences: list[str], max_chars: int = DEFAULT_PAGE_CHARS
) -> list[list[str]]:
    """Group sentences into pages of roughly `max_chars` characters."""
    pages: list[list[str]] = []
    current: list[str] = []
    size = 0

    for sentence in sentences:
        for piece in split_long(sentence, max_chars):
            if current and size + len(piece) + 1 > max_chars:
                pages.append(current)
                current = []
                size = 0
            current.append(piece)
            size += len(piece) + 1

    if current:
        pages.append(current)
    return pages


def count_pages(text: str, max_chars: int = DEFAULT_PAGE_CHARS) -> int:
    """Number of reader pages in `text`; always at least one."""
    return max(1, len(paginate(split_sentences(text), max_chars)))