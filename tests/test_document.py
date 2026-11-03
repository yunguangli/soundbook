from pathlib import Path

import pytest

from soundbook.core.document import parse_book, parse_md, parse_txt


def test_parse_txt_single_chapter(tmp_path: Path):
    path = tmp_path / "essay.txt"
    path.write_text("A short essay.\n\nIt has two paragraphs.", encoding="utf-8")

    book = parse_txt(path)
    assert book.title == "essay"
    assert len(book.chapters) == 1
    assert "two paragraphs" in book.chapters[0].text


def test_parse_txt_detects_chapters(tmp_path: Path):
    path = tmp_path / "book.txt"
    path.write_text(
        "Chapter 1\n\nFirst chapter body.\n\n"
        "Chapter 2\n\nSecond chapter body.\n",
        encoding="utf-8",
    )

    book = parse_txt(path)
    assert len(book.chapters) == 2
    assert book.chapters[0].title.startswith("Chapter 1")
    assert "First chapter body" in book.chapters[0].text
    assert "Second chapter body" in book.chapters[1].text


def test_parse_md_uses_headings_as_chapters(tmp_path: Path):
    path = tmp_path / "report.md"
    path.write_text(
        "---\n"
        "title: Sample Report\n"
        "author: Jane Doe\n"
        "---\n\n"
        "An introduction paragraph before the first heading.\n\n"
        "# Overview\n\n"
        "The company builds accelerated computing platforms.\n\n"
        "## Risks\n\n"
        "**Competition** is the main risk [source](https://example.com).\n",
        encoding="utf-8",
    )

    book = parse_md(path)
    assert book.title == "Sample Report"
    assert book.author == "Jane Doe"
    assert [chapter.title for chapter in book.chapters] == [
        "report",
        "Overview",
        "Risks",
    ]
    assert "introduction paragraph" in book.chapters[0].text
    assert "accelerated computing" in book.chapters[1].text
    assert book.chapters[2].text.startswith("Risks")
    assert "Competition" in book.chapters[2].text
    assert "**" not in book.chapters[2].text
    assert "https://" not in book.chapters[2].text


def test_parse_md_without_headings_is_one_chapter(tmp_path: Path):
    path = tmp_path / "notes.md"
    path.write_text(
        "A plain Markdown note with no headings.\n\n"
        "It still has enough text to be read aloud.\n",
        encoding="utf-8",
    )

    book = parse_book(path)
    assert book.title == "notes"
    assert len(book.chapters) == 1
    assert "plain Markdown note" in book.chapters[0].text


def test_parse_md_setext_heading(tmp_path: Path):
    path = tmp_path / "old.md"
    path.write_text(
        "Chapter One\n===========\n\n"
        "Body of the first chapter.\n\n"
        "Chapter Two\n-----------\n\n"
        "Body of the second chapter.\n",
        encoding="utf-8",
    )

    book = parse_md(path)
    assert [chapter.title for chapter in book.chapters] == [
        "Chapter One",
        "Chapter Two",
    ]


def test_parse_md_strips_tables_and_markup(tmp_path: Path):
    path = tmp_path / "table.md"
    path.write_text(
        "# Numbers\n\n"
        "|Year|Sales|\n|---|---|\n|2024|100|\n\n"
        "Final paragraph that is read aloud.\n",
        encoding="utf-8",
    )

    book = parse_md(path)
    assert "|" not in book.chapters[0].text
    assert "2024, 100" in book.chapters[0].text
    assert "Final paragraph" in book.chapters[0].text


def _make_pdf(tmp_path: Path, toc: bool = False) -> Path:
    pymupdf = pytest.importorskip("pymupdf")

    doc = pymupdf.open()
    for index in range(3):
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 30), "US EQUITY RESEARCH", fontsize=10)
        page.insert_text(
            (40, 810),
            "Confidential disclaimer that must never be read aloud.",
            fontsize=8,
        )
        page.insert_text(
            (40, 100),
            f"Body {index + 1} discusses demand for accelerated computing.",
            fontsize=11,
        )
        page.insert_text(
            (40, 130),
            "This paragraph is unique to this page number "
            f"{index + 1} and should survive.",
            fontsize=11,
        )

    page = doc[0]
    page.insert_text((40, 160), "The word exam-\nple is split across lines.", fontsize=11)

    if toc:
        doc.set_toc([[1, "Overview", 1], [1, "Financials", 3]])

    path = tmp_path / "sample.pdf"
    doc.save(path)
    doc.close()
    return path


def test_parse_pdf_removes_furniture_and_repairs_hyphens(tmp_path: Path):
    path = _make_pdf(tmp_path)

    book = parse_book(path)
    text = "\n\n".join(chapter.text for chapter in book.chapters)

    assert "US EQUITY RESEARCH" not in text
    assert "Confidential disclaimer" not in text
    assert "Body 1 discusses" in text
    assert "example is split" in text
    assert len(book.chapters) == 3
    assert [chapter.title for chapter in book.chapters] == [
        "Page 1",
        "Page 2",
        "Page 3",
    ]


def test_parse_pdf_uses_outline_for_chapters(tmp_path: Path):
    path = _make_pdf(tmp_path, toc=True)

    book = parse_book(path)

    assert [chapter.title for chapter in book.chapters] == [
        "Overview",
        "Financials",
    ]
    assert "Body 1 discusses" in book.chapters[0].text
    assert "Body 2 discusses" in book.chapters[0].text
    assert "Body 3 discusses" in book.chapters[1].text


def test_parse_book_rejects_unknown(tmp_path: Path):
    path = tmp_path / "file.mobi"
    path.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_book(path)


def test_parse_book_missing_text(tmp_path: Path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_book(path)
