#!/usr/bin/env python3
"""Convert a PDF into clean Markdown for Soundbook.

This is the heavy, desktop-side step: layout-aware extraction, running
header/footer removal, margin trimming, hyphen repair and (by default) table
dropping. Import the resulting ``.md`` file into the reader app, where every
heading becomes a chapter.

    uv run --with pymupdf4llm python scripts/pdf2text.py report.pdf
    uv run --with pymupdf4llm python scripts/pdf2text.py report.pdf -o book.md
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from soundbook.core.pdf_clean import (  # noqa: E402
    MARGIN_RATIO,
    REPEAT_RATIO,
    clean_markdown_page,
    margin_furniture,
    repeated_lines,
    strip_markup,
)

_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$")
_YAML_SPECIAL = re.compile(r"[:#\[\]{}\"'\\\n]")


def _yaml_scalar(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        return '""'
    if _YAML_SPECIAL.search(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        match = _HEADING.match(line.strip())
        if match:
            return strip_markup(match.group(1)).strip()[:200]
    return ""


def convert(
    pdf: Path,
    output: Path,
    keep_tables: bool = False,
    margin: float = MARGIN_RATIO,
    repeat_ratio: float = REPEAT_RATIO,
) -> tuple[int, int, int]:
    """Write ``output`` and return (pages, chars_before, chars_after)."""
    import pymupdf
    import pymupdf4llm

    doc = pymupdf.open(str(pdf))
    try:
        page_blocks = []
        page_heights = []
        for page in doc:
            blocks = [
                tuple(block[:5])
                for block in page.get_text("blocks")
                if len(block) > 6 and block[6] == 0
            ]
            page_blocks.append(blocks)
            page_heights.append(page.rect.height)

        chunks = pymupdf4llm.to_markdown(
            doc, page_chunks=True, show_progress=False
        )
        raw_pages = [chunk["text"] for chunk in chunks]

        junk = repeated_lines(
            [text.splitlines() for text in raw_pages], repeat_ratio
        )
        junk |= margin_furniture(page_blocks, page_heights, margin)
        junk.discard("")

        cleaned = [
            clean_markdown_page(text, junk, keep_tables=keep_tables)
            for text in raw_pages
        ]
        body = "\n\n".join(page for page in cleaned if page)

        metadata = doc.metadata or {}
        title = (
            (metadata.get("title") or "").strip()
            or _first_heading(body)
            or pdf.stem
        )
        author = (metadata.get("author") or "").strip()
    finally:
        doc.close()

    if not body.strip():
        raise ValueError(
            f"no text extracted from {pdf.name}; it may be a scanned PDF"
        )

    header = ["---", f"title: {_yaml_scalar(title)}"]
    if author:
        header.append(f"author: {_yaml_scalar(author)}")
    header.extend([f"source: {_yaml_scalar(pdf.name)}", "---", ""])
    markdown = "\n".join(header) + "\n" + body.rstrip() + "\n"

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")

    before = sum(len(text) for text in raw_pages)
    return len(raw_pages), before, len(markdown)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output Markdown file (default: alongside the PDF)",
    )
    parser.add_argument(
        "--keep-tables",
        action="store_true",
        help="keep Markdown tables instead of dropping number-only rows",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=MARGIN_RATIO,
        help=f"top/bottom margin band as a fraction (default: {MARGIN_RATIO})",
    )
    parser.add_argument(
        "--min-repeat",
        type=float,
        default=REPEAT_RATIO,
        help=(
            "share of pages a line must appear on to count as a header/footer "
            f"(default: {REPEAT_RATIO})"
        ),
    )
    args = parser.parse_args()

    pdf = args.pdf.expanduser()
    if not pdf.is_file():
        print(f"error: no such file: {pdf}", file=sys.stderr)
        return 1
    if pdf.suffix.lower() != ".pdf":
        print(f"error: expected a PDF, got {pdf.suffix or pdf.name}", file=sys.stderr)
        return 1

    output = args.output or pdf.with_suffix(".md")
    t0 = time.perf_counter()
    try:
        pages, before, after = convert(
            pdf,
            output,
            keep_tables=args.keep_tables,
            margin=args.margin,
            repeat_ratio=args.min_repeat,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"pages : {pages}")
    print(f"text  : {before:,} -> {after:,} chars")
    print(f"time  : {time.perf_counter() - t0:.2f} s")
    print(f"out   : {output}")
    print()
    print("Import the .md file in Soundbook: each heading becomes a chapter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
