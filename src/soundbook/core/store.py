"""SQLite library, reading progress and settings."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from soundbook.core.chunker import count_pages
from soundbook.core.document import Book, Chapter

# Bump when the schema below changes; `Store._migrate` upgrades older files.
# 3: pages grew from 360 to 720 chars, so page counts must be recomputed.
SCHEMA_VERSION = 3

# Page size used by schema versions 1 and 2, needed to preserve the reading
# percentage when older databases are repaginated.
LEGACY_PAGE_CHARS = 360

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(book_id, idx)
);

CREATE TABLE IF NOT EXISTS progress (
    book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    chapter_idx INTEGER NOT NULL DEFAULT 0,
    page_idx INTEGER NOT NULL DEFAULT 0,
    sentence_idx INTEGER NOT NULL DEFAULT 0,
    fraction REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS text_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.layout_changed = False
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # schema ---------------------------------------------------------------

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Upgrade databases created before `SCHEMA_VERSION`."""
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version >= SCHEMA_VERSION:
            return

        # Page audio is keyed by page number, so changing the page size makes
        # every cached file wrong. The app clears the cache once.
        self.layout_changed = version < 3

        if "page_count" not in self._columns("chapters"):
            self._db.execute(
                "ALTER TABLE chapters ADD COLUMN page_count INTEGER NOT NULL DEFAULT 0"
            )
        if "fraction" not in self._columns("progress"):
            self._db.execute(
                "ALTER TABLE progress ADD COLUMN fraction REAL NOT NULL DEFAULT 0"
            )

        if version < 3:
            self._migrate_page_layout(version)

        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_page_layout(self, version: int) -> None:
        """Repaginate chapters and keep each reader near the same percentage."""
        chapters = self._db.execute(
            "SELECT book_id, idx, text, page_count FROM chapters "
            "ORDER BY book_id, idx"
        ).fetchall()

        old_counts: dict[int, list[int]] = {}
        new_counts: dict[int, list[int]] = {}
        for row in chapters:
            if version >= 2 and int(row["page_count"]) > 0:
                old_count = int(row["page_count"])
            else:
                old_count = count_pages(row["text"], LEGACY_PAGE_CHARS)
            new_count = count_pages(row["text"])

            old_counts.setdefault(row["book_id"], []).append(old_count)
            new_counts.setdefault(row["book_id"], []).append(new_count)
            self._db.execute(
                "UPDATE chapters SET page_count = ? "
                "WHERE book_id = ? AND idx = ?",
                (new_count, row["book_id"], row["idx"]),
            )

        for row in self._db.execute(
            "SELECT book_id, chapter_idx, page_idx, fraction FROM progress"
        ).fetchall():
            book_id = int(row["book_id"])
            old = old_counts.get(book_id) or []
            new = new_counts.get(book_id) or []
            if not old or not new:
                continue

            if version >= 2:
                fraction = float(row["fraction"])
            else:
                chapter_idx = max(
                    0, min(int(row["chapter_idx"]), len(old) - 1)
                )
                page_idx = max(
                    0, min(int(row["page_idx"]), old[chapter_idx] - 1)
                )
                total = sum(old)
                fraction = (
                    (sum(old[:chapter_idx]) + page_idx) / total if total else 0.0
                )
            fraction = max(0.0, min(1.0, fraction))

            total = sum(new)
            if not total:
                chapter_idx = page_idx = 0
                new_fraction = 0.0
            else:
                target = fraction * total
                offset = 0
                chapter_idx = len(new) - 1
                page_idx = 0
                for index, count in enumerate(new):
                    if index == len(new) - 1 or target < offset + count:
                        chapter_idx = index
                        page_idx = max(
                            0,
                            min(count - 1, int(target - offset + 0.5)),
                        )
                        break
                    offset += count
                new_fraction = (offset + page_idx) / total

            self._db.execute(
                "UPDATE progress SET chapter_idx = ?, page_idx = ?, "
                "sentence_idx = 0, fraction = ? WHERE book_id = ?",
                (chapter_idx, page_idx, new_fraction, book_id),
            )

    def _fraction(
        self,
        book_id: int,
        chapter_idx: int,
        page_idx: int,
        page_progress: float,
    ) -> float:
        """Global 0..1 reading position from per-chapter page counts."""
        counts = [
            max(0, int(row["page_count"]))
            for row in self._db.execute(
                "SELECT page_count FROM chapters WHERE book_id = ? ORDER BY idx",
                (book_id,),
            )
        ]
        total = sum(counts)
        if not total:
            return 0.0

        chapter_idx = max(0, min(chapter_idx, len(counts) - 1))
        pages_in_chapter = counts[chapter_idx] or 1
        page_idx = max(0, min(page_idx, pages_in_chapter - 1))
        within = max(0.0, min(1.0, float(page_progress)))

        offset = sum(counts[:chapter_idx])
        return max(0.0, min(1.0, (offset + page_idx + within) / total))

    # books ---------------------------------------------------------------

    def add_book(self, book: Book, source: str) -> int:
        with self._lock:
            existing = self._db.execute(
                "SELECT id FROM books WHERE source = ?", (source,)
            ).fetchone()
            if existing:
                return int(existing["id"])

            cursor = self._db.execute(
                "INSERT INTO books (title, author, source, created_at) VALUES (?, ?, ?, ?)",
                (book.title, book.author, source, time.time()),
            )
            book_id = int(cursor.lastrowid)
            self._db.executemany(
                "INSERT INTO chapters (book_id, idx, title, text, page_count) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        book_id,
                        index,
                        chapter.title,
                        chapter.text,
                        count_pages(chapter.text),
                    )
                    for index, chapter in enumerate(book.chapters)
                ],
            )
            self._db.commit()
            return book_id

    def list_books(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._db.execute(
                    """
                    SELECT b.*,
                           COALESCE(p.chapter_idx, 0) AS chapter_idx,
                           COALESCE(p.page_idx, 0) AS page_idx,
                           COALESCE(p.fraction, 0.0) AS fraction,
                           COALESCE(p.updated_at, 0.0) AS updated_at,
                           (SELECT COUNT(*) FROM chapters c
                            WHERE c.book_id = b.id) AS chapter_count,
                           (SELECT COALESCE(SUM(page_count), 0) FROM chapters c
                            WHERE c.book_id = b.id) AS page_count,
                           (SELECT COALESCE(SUM(page_count), 0) FROM chapters c
                            WHERE c.book_id = b.id
                              AND c.idx < COALESCE(p.chapter_idx, 0)) AS page_offset
                    FROM books b
                    LEFT JOIN progress p ON p.book_id = b.id
                    ORDER BY b.created_at DESC
                    """
                )
            )

    def get_book(self, book_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM books WHERE id = ?", (book_id,)
            ).fetchone()

    def chapters(self, book_id: int) -> list[Chapter]:
        with self._lock:
            rows = self._db.execute(
                "SELECT title, text FROM chapters WHERE book_id = ? ORDER BY idx",
                (book_id,),
            )
            return [Chapter(title=row["title"], text=row["text"]) for row in rows]

    def delete_book(self, book_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM books WHERE id = ?", (book_id,))
            self._db.commit()

    # progress ------------------------------------------------------------

    def save_progress(
        self,
        book_id: int,
        chapter_idx: int,
        page_idx: int,
        sentence_idx: int = 0,
        page_progress: float = 0.0,
    ) -> None:
        """Persist position; `page_progress` is 0..1 within the current page."""
        with self._lock:
            fraction = self._fraction(
                book_id, chapter_idx, page_idx, page_progress
            )
            self._db.execute(
                """
                INSERT INTO progress
                    (book_id, chapter_idx, page_idx, sentence_idx, fraction, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id) DO UPDATE SET
                    chapter_idx = excluded.chapter_idx,
                    page_idx = excluded.page_idx,
                    sentence_idx = excluded.sentence_idx,
                    fraction = excluded.fraction,
                    updated_at = excluded.updated_at
                """,
                (
                    book_id,
                    chapter_idx,
                    page_idx,
                    sentence_idx,
                    fraction,
                    time.time(),
                ),
            )
            self._db.commit()

    def touch_progress(self, book_id: int) -> None:
        """Mark a book as opened without moving the saved position."""
        with self._lock:
            self._db.execute(
                """
                INSERT INTO progress
                    (book_id, chapter_idx, page_idx, sentence_idx, fraction, updated_at)
                VALUES (?, 0, 0, 0, 0, ?)
                ON CONFLICT(book_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (book_id, time.time()),
            )
            self._db.commit()

    def get_progress(self, book_id: int) -> tuple[int, int, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT chapter_idx, page_idx, sentence_idx FROM progress WHERE book_id = ?",
                (book_id,),
            ).fetchone()
        if row is None:
            return (0, 0, 0)
        return (row["chapter_idx"], row["page_idx"], row["sentence_idx"])

    # typed text history --------------------------------------------------

    def add_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._lock:
            last = self._db.execute(
                "SELECT id, text FROM text_history ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if last and last["text"] == text:
                self._db.execute(
                    "UPDATE text_history SET created_at = ? WHERE id = ?",
                    (time.time(), last["id"]),
                )
            else:
                self._db.execute(
                    "INSERT INTO text_history (text, created_at) VALUES (?, ?)",
                    (text, time.time()),
                )
            self._db.commit()

    def list_texts(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._db.execute(
                    "SELECT id, text, created_at FROM text_history "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            )

    def delete_text(self, text_id: int) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM text_history WHERE id = ?", (text_id,)
            )
            self._db.commit()

    # settings ------------------------------------------------------------

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._db.commit()