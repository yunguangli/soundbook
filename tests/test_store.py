import sqlite3

from soundbook.core.chunker import count_pages
from soundbook.core.document import Book, Chapter
from soundbook.core.store import Store


def _book():
    return Book(
        title="Test Book",
        author="Someone",
        chapters=[Chapter("One", "First chapter."), Chapter("Two", "Second.")],
    )


def test_add_and_list_book(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    book_id = store.add_book(_book(), source="/tmp/test.epub")

    books = store.list_books()
    assert len(books) == 1
    assert books[0]["title"] == "Test Book"
    assert books[0]["chapter_idx"] == 0

    # Re-importing the same source is idempotent.
    assert store.add_book(_book(), source="/tmp/test.epub") == book_id
    store.close()


def test_chapters(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    book_id = store.add_book(_book(), source="/tmp/test.epub")

    chapters = store.chapters(book_id)
    assert [chapter.title for chapter in chapters] == ["One", "Two"]
    assert chapters[1].text == "Second."
    store.close()


def test_progress(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    book_id = store.add_book(_book(), source="/tmp/test.epub")

    assert store.get_progress(book_id) == (0, 0, 0)
    store.save_progress(book_id, 1, 4, 2)
    assert store.get_progress(book_id) == (1, 4, 2)
    store.close()


def test_settings(tmp_path):
    store = Store(tmp_path / "db.sqlite3")

    assert store.get_setting("voice", "af_heart") == "af_heart"
    store.set_setting("voice", "bf_emma")
    assert store.get_setting("voice") == "bf_emma"
    store.close()


LONG_TEXT = "A sentence with several words in it. " * 120


def _long_book() -> Book:
    return Book(
        title="Long",
        author="",
        chapters=[Chapter("One", LONG_TEXT), Chapter("Two", LONG_TEXT)],
    )


V1_SCHEMA = """
CREATE TABLE books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(book_id, idx)
);
CREATE TABLE progress (
    book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    chapter_idx INTEGER NOT NULL DEFAULT 0,
    page_idx INTEGER NOT NULL DEFAULT 0,
    sentence_idx INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""


def test_page_counts_and_progress_fraction(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    pages = count_pages(LONG_TEXT)
    assert pages > 1

    book_id = store.add_book(_long_book(), source="/tmp/long.txt")
    row = store.list_books()[0]
    assert row["page_count"] == pages * 2
    assert row["page_offset"] == 0

    store.save_progress(book_id, 0, 0, 0, page_progress=0.0)
    assert store.list_books()[0]["fraction"] == 0.0

    store.save_progress(book_id, 0, 0, 0, page_progress=1.0)
    assert abs(store.list_books()[0]["fraction"] - 1 / (pages * 2)) < 1e-9

    store.save_progress(book_id, 1, 0, 0)
    row = store.list_books()[0]
    assert row["page_offset"] == pages
    assert abs(row["fraction"] - 0.5) < 1e-9

    store.save_progress(book_id, 1, pages - 1, 0, page_progress=1.0)
    assert abs(store.list_books()[0]["fraction"] - 1.0) < 1e-9
    store.close()


def test_touch_progress_creates_row_without_moving(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    book_id = store.add_book(_book(), source="/tmp/touch.epub")

    assert store.list_books()[0]["updated_at"] == 0
    store.touch_progress(book_id)

    row = store.list_books()[0]
    assert row["updated_at"] > 0
    assert row["fraction"] == 0.0
    assert store.get_progress(book_id) == (0, 0, 0)
    store.close()


def test_migrates_v1_database(tmp_path):
    path = tmp_path / "v1.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(V1_SCHEMA)
    db.execute(
        "INSERT INTO books (id, title, author, source, created_at) "
        "VALUES (1, 'Old', '', '/tmp/old.txt', 0)"
    )
    db.execute(
        "INSERT INTO chapters (book_id, idx, title, text) VALUES (1, 0, 'One', ?)",
        (LONG_TEXT,),
    )
    db.execute(
        "INSERT INTO progress "
        "(book_id, chapter_idx, page_idx, sentence_idx, updated_at) "
        "VALUES (1, 0, 1, 0, 0)"
    )
    db.commit()
    db.close()

    store = Store(path)
    row = store.list_books()[0]
    assert row["page_count"] == count_pages(LONG_TEXT)
    assert row["page_count"] > 1
    assert abs(row["fraction"] - 1 / row["page_count"]) < 1e-9
    assert store.layout_changed is True
    store.close()


V2_SCHEMA = V1_SCHEMA.replace(
    "    text TEXT NOT NULL,\n",
    "    text TEXT NOT NULL,\n    page_count INTEGER NOT NULL DEFAULT 0,\n",
).replace(
    "    sentence_idx INTEGER NOT NULL DEFAULT 0,\n",
    "    sentence_idx INTEGER NOT NULL DEFAULT 0,\n"
    "    fraction REAL NOT NULL DEFAULT 0,\n",
)


def test_migrates_v2_to_larger_pages(tmp_path):
    path = tmp_path / "v2.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(V2_SCHEMA)
    db.execute("PRAGMA user_version = 2")
    db.execute(
        "INSERT INTO books (id, title, author, source, created_at) "
        "VALUES (1, 'Old', '', '/tmp/old.txt', 0)"
    )
    db.execute(
        "INSERT INTO chapters "
        "(book_id, idx, title, text, page_count) VALUES (1, 0, 'One', ?, 999)",
        (LONG_TEXT,),
    )
    db.execute(
        "INSERT INTO progress "
        "(book_id, chapter_idx, page_idx, sentence_idx, fraction, updated_at) "
        "VALUES (1, 0, 0, 0, 0.5, 0)"
    )
    db.commit()
    db.close()

    store = Store(path)
    row = store.list_books()[0]
    assert store.layout_changed is True
    assert row["page_count"] == count_pages(LONG_TEXT)
    assert row["page_count"] < 999
    # The position is mapped to the nearest new page, so the percentage stays
    # close to the old 50%.
    assert 0.5 <= row["fraction"] <= 0.65
    store.close()