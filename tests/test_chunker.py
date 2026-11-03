from soundbook.core.chunker import (
    count_pages,
    paginate,
    split_long,
    split_sentences,
)


def test_split_sentences_basic():
    text = "One sentence. Two sentences! A third?"
    assert split_sentences(text) == [
        "One sentence.",
        "Two sentences!",
        "A third?",
    ]


def test_split_sentences_keeps_abbreviations():
    result = split_sentences("Mr. Smith went home. He slept.")
    assert result == ["Mr. Smith went home.", "He slept."]


def test_split_sentences_handles_paragraphs():
    text = "First paragraph.\n\nSecond paragraph."
    assert split_sentences(text) == ["First paragraph.", "Second paragraph."]


def test_split_long_breaks_on_words():
    chunks = split_long("word " * 20, max_chars=30)
    assert len(chunks) > 1
    assert all(len(chunk) <= 30 for chunk in chunks)


def test_paginate_respects_budget():
    sentences = ["a" * 40, "b" * 40, "c" * 40]
    pages = paginate(sentences, max_chars=100)
    assert len(pages) == 2
    assert pages[0] == ["a" * 40, "b" * 40]
    assert pages[1] == ["c" * 40]


def test_paginate_empty():
    assert paginate([]) == []


def test_count_pages_is_at_least_one():
    assert count_pages("") == 1
    assert count_pages("One sentence. Two sentences.") == 1
    assert count_pages("A sentence with words. " * 100) > 1