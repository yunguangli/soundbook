from soundbook.core.pdf_clean import (
    clean_markdown_page,
    drop_table_rows,
    is_table_line,
    repeated_lines,
    repair_hyphenation,
    signature,
    strip_markup,
)


def test_repair_hyphenation():
    assert repair_hyphenation("exam-\nple") == "example"
    assert repair_hyphenation("exam- \n  ple") == "example"
    assert repair_hyphenation("well - known") == "well - known"


def test_repeated_lines_uses_page_share():
    pages = [
        ["US EQUITY RESEARCH", "first page body"],
        ["US EQUITY RESEARCH", "second page body"],
        ["US EQUITY RESEARCH", "third page body"],
    ]
    junk = repeated_lines(pages, ratio=0.5)

    assert signature("US EQUITY RESEARCH") in junk
    assert signature("first page body") not in junk


def test_table_line_detection():
    assert is_table_line("P/E (x) 164.5 71.2 44.8 24.1")
    assert is_table_line("FY Apr FY2024(A) FY2025(A) FY2026(A) FY2027(F)")
    assert not is_table_line("Revenue grew 12% in 2024 to 78 billion.")
    assert not is_table_line(
        "Sales rose 60,922 130,497 215,938 and 392,144."
    )
    assert not is_table_line(
        "The Company accelerates computing to help solve computational problems."
    )


def test_drop_table_rows_keeps_paragraphs():
    paragraph = (0, 100, 500, 130, "A normal paragraph of body text.")
    sales = [
        (0, 200, 40, 210, "Sales"),
        (60, 200, 120, 210, "60,922"),
        (140, 200, 210, 210, "130,497"),
        (230, 200, 310, 210, "215,938"),
        (330, 200, 400, 210, "392,144"),
    ]

    kept = drop_table_rows([paragraph, *sales])

    assert paragraph in kept
    assert all(block not in kept for block in sales)


def test_strip_markup():
    text = "**Bold** and [a link](https://example.com) with `code`."
    assert strip_markup(text) == "Bold and a link with code."


def test_clean_markdown_page():
    page = "\n".join(
        [
            "US EQUITY RESEARCH",
            "",
            "### **Financial Summary**",
            "",
            "|Sales|60,922|130,497|",
            "|---|---|---|",
            "",
            "Real body text that should survive.",
            "",
            "<!-- Start of picture text -->",
            "380 280 180 80",
            "<!-- End of picture text -->",
        ]
    )
    junk = {signature("US EQUITY RESEARCH")}

    cleaned = clean_markdown_page(page, junk)

    assert "US EQUITY RESEARCH" not in cleaned
    assert "Financial Summary" in cleaned
    assert "60,922" not in cleaned
    assert "Real body text" in cleaned
    assert "380 280" not in cleaned
