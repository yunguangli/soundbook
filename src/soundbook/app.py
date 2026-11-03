"""Soundbook Flet app."""

from __future__ import annotations

import asyncio
import os
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

import flet as ft
from flet_audio import Audio, AudioState, ReleaseMode

from soundbook import config
from soundbook.core.audio import PageAudio, PageCache, assemble
from soundbook.core.chunker import paginate, split_sentences
from soundbook.core.document import Chapter, parse_book
from soundbook.core.store import Store
from soundbook.core.tts.voices import list_voices
from soundbook.core.webpage import normalize_url, parse_url

LANGUAGES = {
    "a": "English US",
    "b": "English UK",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Portuguese BR",
    "z": "Chinese",
}

# Dictionary G2P is bundled only for English right now.
SPEAKABLE = {"a", "b"}

HIGHLIGHT = ft.Colors.with_opacity(0.25, ft.Colors.AMBER)

# How many pages ahead to synthesize while the reader is playing.
PREFETCH_AHEAD = 4
# Pages that may be rendered concurrently. Saving still happens per page, as
# soon as that page's own sentences are done.
PREFETCH_PAGES = 2
# Sentences synthesized in parallel. On a 4-thread CPU this is ~2.4x faster
# than synthesizing one sentence at a time.
PARALLEL_SYNTH = min(4, os.cpu_count() or 2)

# The ONNX session is read-only and thread-safe; sharing it between Flet
# sessions avoids loading the 89 MB model again when a page reconnects.
_TTS_MODELS: dict[str, object] = {}
_TTS_MODELS_LOCK = threading.Lock()


def log(message: str) -> None:
    print(f"[soundbook] {message}", flush=True)


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class SoundbookApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        page.title = "Soundbook"
        page.theme_mode = ft.ThemeMode.SYSTEM
        page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
        page.padding = 0
        page.spacing = 0

        self.store = Store(config.db_path())
        self.cache = PageCache(config.cache_dir())
        if self.store.layout_changed:
            # Page size changed, so every cached page number points at the
            # wrong text. Old audio must not be played.
            freed = self.cache.clear()
            log(f"page layout changed; cleared {freed / 1_000_000:.1f} MB cache")
        self.voice = self.store.get_setting("voice", "af_heart")
        self.speed = float(self.store.get_setting("speed", "1.0"))

        self.tts = None
        self._tts_language: str | None = None
        self._busy = False
        self._synthesizing = False
        self._prefetching = False
        self._model_lock = asyncio.Lock()
        self._synth_slots = asyncio.Semaphore(PARALLEL_SYNTH)
        self._prefetch_slots = asyncio.Semaphore(max(1, PARALLEL_SYNTH - 1))
        self._prefetch_tasks: dict[tuple, asyncio.Task] = {}
        self._state = AudioState.STOPPED
        self._active = 0
        self._new_page = True
        self._seeking = False
        self._audio_key: tuple | None = None
        self._advancing = False
        self._polling = False
        self._wants_play = False
        self._view = "library"
        self._closing = False
        self._audio_loaded = asyncio.Event()
        self._warm_started = False

        self.book = None
        self.transient = False
        self.chapters: list[Chapter] = []
        self.pages: list[list[str]] = []
        self.chapter_idx = 0
        self.page_idx = 0
        self.page_audio: PageAudio | None = None
        self._texts: list[ft.Text] = []

        self.file_picker = ft.FilePicker()
        page.on_disconnect = self._on_page_gone
        page.on_close = self._on_page_gone
        page.on_connect = self._on_page_connected
        # Created lazily with a real src: the client rejects an Audio service
        # mounted with src=None, and later updates then never apply.
        self.audio: Audio | None = None
        page.services.append(self.file_picker)

        self.nav = ft.NavigationBar(
            selected_index=0,
            destinations=[
                ft.NavigationBarDestination(
                    icon=ft.Icons.LIBRARY_BOOKS, label="Library"
                ),
                ft.NavigationBarDestination(
                    icon=ft.Icons.EDIT_NOTE, label="Type"
                ),
                ft.NavigationBarDestination(
                    icon=ft.Icons.SETTINGS, label="Settings"
                ),
            ],
            on_change=self._on_nav,
        )
        self.content = ft.Container(expand=True)
        page.add(
            ft.SafeArea(
                expand=True,
                content=ft.Column(
                    [self.content, self.nav], expand=True, spacing=0
                ),
            )
        )
        self.show_library()
        # Warm the TTS model in the background so the first "play" does not
        # pay the 3-4 s model load while the reader waits.
        self._run_task(self._warm_model)

    async def _warm_model(self) -> None:
        if self._warm_started or self.tts is not None or self._closing:
            return
        self._warm_started = True
        if not config.model_path().exists():
            return
        voice = self.voice
        language = "en-gb" if voice.startswith("b") else "en-us"
        cached = _TTS_MODELS.get(language)
        if cached is not None:
            self.tts = cached
            self._tts_language = language
            return

        log("preloading model")
        t0 = time.perf_counter()
        try:
            await self._ensure_model(voice)
            log(f"model preloaded in {time.perf_counter() - t0:.1f}s")
        except Exception as exc:
            # A missing/broken model must not break the library screen;
            # playback will report the error when the user asks for it.
            log(f"model preload failed: {exc}")

    async def _ensure_model(self, voice: str):
        language = "en-gb" if voice.startswith("b") else "en-us"
        if self.tts is not None and self._tts_language == language:
            return self.tts

        async with self._model_lock:
            if self.tts is not None and self._tts_language == language:
                return self.tts
            log("loading model")
            t0 = time.perf_counter()
            tts = await asyncio.to_thread(self._load_tts, voice)
            self.tts = tts
            self._tts_language = language
            log(f"model loaded in {time.perf_counter() - t0:.1f}s")
            return tts

    # navigation ------------------------------------------------------------

    def _on_nav(self, e) -> None:
        index = e.control.selected_index
        if index == 0:
            self.show_library()
        elif index == 1:
            self.show_type()
        else:
            self.show_settings()

    def show_library(self, e=None) -> None:
        self._view = "library"
        self.nav.selected_index = 0
        books = self.store.list_books()
        tiles = [self._book_tile(book) for book in books]

        if tiles:
            body: ft.Control = ft.ListView(tiles, expand=True, spacing=8)
        else:
            body = ft.Container(
                expand=True,
                alignment=ft.Alignment.CENTER,
                content=ft.Column(
                    [
                        ft.Icon(
                            ft.Icons.MENU_BOOK,
                            size=56,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Text(
                            "No books yet",
                            size=18,
                            weight=ft.FontWeight.BOLD,
                        ),
                        ft.Text(
                            "Import a file, or paste a web link to read an article.",
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                ),
            )

        recent = max(
            (
                book
                for book in books
                if book["updated_at"] > 0 and book["fraction"] < 0.999
            ),
            key=lambda book: book["updated_at"],
            default=None,
        )

        controls: list[ft.Control] = [
            ft.Row(
                [
                    ft.Text(
                        "Soundbook",
                        size=26,
                        weight=ft.FontWeight.BOLD,
                    ),
                    ft.Row(
                        [
                            ft.IconButton(
                                ft.Icons.ADD_LINK,
                                tooltip="Read a web link",
                                on_click=self._open_link_dialog,
                            ),
                            ft.IconButton(
                                ft.Icons.ADD,
                                tooltip="Import",
                                on_click=self.import_books,
                            ),
                        ],
                        spacing=0,
                        tight=True,
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            )
        ]
        if recent is not None:
            controls.append(self._continue_card(recent))
        controls.extend(
            [
                ft.Text(
                    "Recently imported files",
                    size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
                body,
            ]
        )

        self.content.content = ft.Container(
            expand=True,
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            content=ft.Column(controls, expand=True, spacing=8),
        )
        self._safe_update()

    def _page_position(self, book) -> tuple[int, int]:
        """1-based current page and total pages for a library row."""
        total = int(book["page_count"] or 0) or 1
        current = int(book["page_offset"] or 0) + int(book["page_idx"] or 0) + 1
        return max(1, min(current, total)), total

    def _book_source_label(self, book) -> str:
        source = book["source"] or ""
        total = int(book["page_count"] or 0) or 1
        pages = f"{total} page{'s' if total != 1 else ''}"

        if source.startswith(("http://", "https://")):
            host = urlparse(source).netloc.removeprefix("www.")
            return f"Web · {host} · {pages}"
        if book["author"]:
            return book["author"]
        kind = Path(source).suffix.lstrip(".").upper() or "TXT"
        return f"{kind} · {pages}"

    def _continue_card(self, book) -> ft.Control:
        current, total = self._page_position(book)
        fraction = float(book["fraction"] or 0.0)

        return ft.Container(
            padding=12,
            border_radius=14,
            bgcolor=ft.Colors.with_opacity(0.10, ft.Colors.PRIMARY),
            on_click=lambda e, book_id=book["id"]: self.open_book(book_id),
            content=ft.Row(
                [
                    ft.Container(
                        width=42,
                        height=42,
                        border_radius=21,
                        bgcolor=ft.Colors.PRIMARY,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(
                            ft.Icons.PLAY_ARROW, color=ft.Colors.ON_PRIMARY
                        ),
                    ),
                    ft.Column(
                        [
                            ft.Text(
                                "Continue reading",
                                size=11,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                            ft.Text(
                                book["title"],
                                weight=ft.FontWeight.BOLD,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                f"Page {current} of {total} · {fraction:.0%}",
                                size=11,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                        ],
                        expand=True,
                        spacing=0,
                    ),
                ],
                spacing=12,
            ),
        )

    def _book_tile(self, book) -> ft.Control:
        fraction = float(book["fraction"] or 0.0)
        started = book["updated_at"] > 0
        current, total = self._page_position(book)

        subtitle = self._book_source_label(book)
        if started:
            subtitle = f"{subtitle} · Page {current} of {total} · {fraction:.0%}"

        def open_book(e=None) -> None:
            self.open_book(book["id"])

        def remove(e=None) -> None:
            self._confirm_remove(book["id"], book["title"])

        return ft.Container(
            padding=14,
            border_radius=14,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            on_click=open_book,
            content=ft.Row(
                [
                    ft.Container(
                        width=48,
                        height=64,
                        border_radius=8,
                        bgcolor=ft.Colors.with_opacity(
                            0.15, ft.Colors.PRIMARY
                        ),
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(ft.Icons.MENU_BOOK),
                    ),
                    ft.Column(
                        [
                            ft.Text(
                                book["title"],
                                weight=ft.FontWeight.BOLD,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                subtitle,
                                size=12,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.ProgressBar(
                                value=fraction,
                                bar_height=4,
                                border_radius=2,
                                color=ft.Colors.PRIMARY,
                                bgcolor=ft.Colors.with_opacity(
                                    0.15, ft.Colors.PRIMARY
                                ),
                            ),
                        ],
                        expand=True,
                        spacing=4,
                    ),
                    ft.Row(
                        [
                            ft.IconButton(
                                ft.Icons.PLAY_ARROW,
                                tooltip=(
                                    "Continue reading"
                                    if started
                                    else "Start reading"
                                ),
                                on_click=open_book,
                            ),
                            ft.PopupMenuButton(
                                icon=ft.Icons.MORE_VERT,
                                items=[
                                    ft.PopupMenuItem(
                                        icon=ft.Icons.DELETE_OUTLINE,
                                        content=ft.Text(
                                            "Remove from library"
                                        ),
                                        on_click=remove,
                                    )
                                ],
                            ),
                        ],
                        spacing=0,
                        tight=True,
                    ),
                ],
                spacing=14,
            ),
        )

    def _confirm_remove(self, book_id: int, title: str) -> None:
        def close(e=None) -> None:
            self.page.pop_dialog()

        def remove(e=None) -> None:
            self.page.pop_dialog()
            self._remove_book(book_id, title)

        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text("Remove book?"),
                content=ft.Text(
                    f"“{title}” and its cached audio will be deleted. "
                    "This cannot be undone."
                ),
                actions=[
                    ft.TextButton("Cancel", on_click=close),
                    ft.FilledButton("Remove", on_click=remove),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
        )

    def _remove_book(self, book_id: int, title: str) -> None:
        self.store.delete_book(book_id)
        self.cache.delete_book(book_id)
        if self.book is not None and self.book["id"] == book_id:
            self._run_task(self._pause_audio)
        self._snack(f"Removed “{title}”")
        self.show_library()

    async def _pause_audio(self) -> None:
        if self.audio is not None:
            try:
                await asyncio.wait_for(self.audio.pause(), timeout=1.0)
            except Exception:
                pass

    # typed text ------------------------------------------------------------

    def show_type(self, e=None) -> None:
        self._view = "type"
        self.nav.selected_index = 1
        current = self.type_field.value if hasattr(self, "type_field") else ""

        self.type_field = ft.TextField(
            value=current,
            multiline=True,
            min_lines=6,
            max_lines=10,
            text_size=16,
            hint_text="Type or paste a paragraph, then tap Read.",
            border_radius=12,
            filled=True,
            on_change=self._draft_changed,
        )
        self.draft_count = ft.Text(
            f"{len(current)} characters",
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )

        history = self.store.list_texts()
        rows = [self._history_tile(item) for item in history]
        if rows:
            history_body: ft.Control = ft.ListView(rows, expand=True, spacing=2)
        else:
            history_body = ft.Container(
                expand=True,
                alignment=ft.Alignment.CENTER,
                content=ft.Text(
                    "Nothing read yet.",
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    size=12,
                ),
            )

        self.content.content = ft.Container(
            expand=True,
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            content=ft.Column(
                [
                    ft.Text(
                        "Read your text", size=26, weight=ft.FontWeight.BOLD
                    ),
                    ft.Text(
                        "Paste an article, a note or a few sentences.",
                        size=12,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    self.type_field,
                    ft.Row(
                        [
                            self.draft_count,
                            ft.Text(
                                f"{self.voice}  ·  {self.speed:.2f}x",
                                size=11,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        [
                            ft.OutlinedButton(
                                "Clear",
                                icon=ft.Icons.CLEAR,
                                on_click=self._clear_type,
                            ),
                            ft.FilledButton(
                                "Read",
                                icon=ft.Icons.GRAPHIC_EQ,
                                on_click=self.read_typed,
                                expand=True,
                            ),
                        ],
                        spacing=8,
                    ),
                    ft.Divider(),
                    ft.Text(
                        "Recent",
                        size=12,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    history_body,
                ],
                expand=True,
                spacing=10,
            ),
        )
        self._safe_update()

    def _history_tile(self, item) -> ft.Control:
        preview = " ".join(item["text"].split())
        if len(preview) > 90:
            preview = preview[:87] + "…"
        when = time.strftime(
            "%d %b · %H:%M", time.localtime(item["created_at"])
        )
        return ft.ListTile(
            title=ft.Text(
                preview, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS
            ),
            subtitle=ft.Text(when, size=11),
            on_click=lambda e, text=item["text"]: self._load_history(text),
            trailing=ft.IconButton(
                ft.Icons.DELETE_OUTLINE,
                tooltip="Delete",
                on_click=lambda e, text_id=item["id"]: self._delete_history(
                    text_id
                ),
            ),
        )

    def _draft_changed(self, e) -> None:
        self.draft_count.value = f"{len(e.control.value or '')} characters"
        self._safe_update()

    def _load_history(self, text: str) -> None:
        self.type_field.value = text
        self.draft_count.value = f"{len(text)} characters"
        self._safe_update()

    def _delete_history(self, text_id: int) -> None:
        self.store.delete_text(text_id)
        self.show_type()

    def _clear_type(self, e=None) -> None:
        self.type_field.value = ""
        self.draft_count.value = "0 characters"
        self._safe_update()

    def read_typed(self, e=None) -> None:
        text = (self.type_field.value or "").strip()
        if not text:
            self._snack("Type something first.")
            return
        self.store.add_text(text)
        self.open_text(text)

    def open_text(self, text: str) -> None:
        self.transient = True
        self.book = {
            "id": -int(time.time() * 1000),
            "title": "Typed text",
            "author": "",
        }
        self.chapters = [Chapter("Typed text", text)]
        self.chapter_idx = 0
        self.load_page(0, 0)

    def go_back(self, e=None) -> None:
        if self.transient:
            self.show_type()
        else:
            self.show_library()

    # import ----------------------------------------------------------------

    async def import_books(self, e=None) -> None:
        files = await self.file_picker.pick_files(
            dialog_title="Import a book",
            allow_multiple=True,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=[
                "epub",
                "pdf",
                "txt",
                "md",
                "markdown",
                "html",
                "htm",
                "xhtml",
            ],
        )
        if not files:
            return

        imported = 0
        errors: list[str] = []
        for file in files:
            try:
                path = await self._materialize(file)
                book = await asyncio.to_thread(parse_book, path)
                await asyncio.to_thread(self.store.add_book, book, str(path))
                imported += 1
            except Exception as exc:
                errors.append(f"{file.name}: {exc}")

        if errors:
            self._snack("; ".join(errors))
        else:
            self._snack(f"Imported {imported} book(s)")
        self.show_library()

    async def _materialize(self, file) -> Path:
        if file.path:
            return Path(file.path)
        if not file.bytes:
            raise ValueError("no file data")
        target = config.data_dir() / "imports" / file.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.bytes)
        return target

    def _open_link_dialog(self, e=None) -> None:
        field = ft.TextField(
            label="Web link",
            hint_text="https://example.com/article",
            autofocus=True,
            keyboard_type=ft.KeyboardType.URL,
            on_submit=lambda _event: submit(),
        )

        def close(event=None) -> None:
            self.page.pop_dialog()

        def submit(event=None) -> None:
            url = (field.value or "").strip()
            if not url:
                self._snack("Paste a link first.")
                return
            self.page.pop_dialog()
            self._run_task(self._import_link, url)

        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text("Read a web link"),
                content=ft.Column(
                    [
                        field,
                        ft.Text(
                            "Only the main article is imported; navigation, "
                            "ads and comments are skipped.",
                            size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                    tight=True,
                    spacing=8,
                ),
                actions=[
                    ft.TextButton("Cancel", on_click=close),
                    ft.FilledButton("Import", on_click=submit),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
        )

    async def _import_link(self, url: str) -> None:
        try:
            url = normalize_url(url)
        except ValueError as exc:
            self._show_error("Could not import link", str(exc))
            return

        log(f"importing link: {url}")
        self._show_link_progress(url)
        try:
            book, source = await asyncio.to_thread(parse_url, url)
            book_id = await asyncio.to_thread(self.store.add_book, book, source)
        except Exception as exc:
            log(f"link import failed: {exc}")
            self._close_dialog()
            self._show_error("Could not import link", str(exc))
            return

        self._close_dialog()
        self.show_library()
        self.open_book(book_id)
        self._snack(f"Imported “{book.title}”")

    def _show_link_progress(self, url: str) -> None:
        if self._closing:
            return
        try:
            self.page.show_dialog(
                ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Reading link"),
                    content=ft.Row(
                        [
                            ft.ProgressRing(
                                width=18,
                                height=18,
                                stroke_width=2,
                                color=ft.Colors.PRIMARY,
                            ),
                            ft.Text(
                                url,
                                size=12,
                                max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS,
                                expand=True,
                            ),
                        ],
                        spacing=12,
                        tight=True,
                    ),
                )
            )
        except RuntimeError:
            self._closing = True

    def _show_error(self, title: str, message: str) -> None:
        if self._closing:
            return
        try:
            self.page.show_dialog(
                ft.AlertDialog(
                    modal=True,
                    title=ft.Text(title),
                    content=ft.Text(message),
                    actions=[
                        ft.TextButton("OK", on_click=lambda e: self._close_dialog())
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
            )
        except RuntimeError:
            self._closing = True

    def _close_dialog(self) -> None:
        try:
            self.page.pop_dialog()
        except RuntimeError:
            self._closing = True

    def _on_page_gone(self, e=None) -> None:
        """Stop background work when the Flet session is destroyed."""
        self._closing = True
        self._wants_play = False

    def _on_page_connected(self, e=None) -> None:
        self._closing = False

    def _safe_update(self) -> None:
        if self._closing:
            return
        try:
            self.page.update()
        except RuntimeError:
            # The session went away while a background task was finishing.
            self._closing = True

    def _run_task(self, handler, *args) -> None:
        if self._closing:
            return
        try:
            self.page.run_task(handler, *args)
        except RuntimeError:
            self._closing = True

    def _snack(self, message: str) -> None:
        if self._closing:
            return
        try:
            self.page.show_dialog(ft.SnackBar(content=ft.Text(message)))
        except RuntimeError:
            self._closing = True

    # reader ----------------------------------------------------------------

    def open_book(self, book_id: int) -> None:
        self.transient = False
        self.book = self.store.get_book(book_id)
        self.chapters = self.store.chapters(book_id)
        if not self.chapters:
            self._snack("This book has no readable chapters.")
            return

        chapter_idx, page_idx, sentence_idx = self.store.get_progress(book_id)
        self.chapter_idx = min(max(chapter_idx, 0), len(self.chapters) - 1)
        self.load_page(
            self.chapter_idx, page_idx, sentence_idx, save=False
        )
        self.store.touch_progress(book_id)

    def load_page(
        self,
        chapter_idx: int,
        page_idx: int,
        sentence_idx: int = 0,
        save: bool = True,
    ) -> None:
        if not self.chapters or self.book is None:
            return

        self.chapter_idx = max(0, min(chapter_idx, len(self.chapters) - 1))
        chapter = self.chapters[self.chapter_idx]
        self.pages = paginate(split_sentences(chapter.text)) or [[]]
        self.page_idx = max(0, min(page_idx, len(self.pages) - 1))
        sentences = self.pages[self.page_idx]

        self._active = (
            max(0, min(sentence_idx, len(sentences) - 1)) if sentences else 0
        )
        self._new_page = True
        self.page_audio = self.cache.load(
            self.book["id"],
            self.chapter_idx,
            self.page_idx,
            self.voice,
            self.speed,
        )
        if save and not self.transient:
            self._save_progress(self._active)
        self._render_reader()

        # Start rendering right away so the first play is not a long wait.
        if self.page_audio is None:
            self._wants_play = False
            self._run_task(self._prepare_page)

    def _render_reader(self) -> None:
        asserts_book = self.book is not None and self.chapters
        if not asserts_book:
            return

        self._view = "reader"
        sentences = self.pages[self.page_idx]
        self._texts = []
        for index, sentence in enumerate(sentences):
            text = ft.Text(
                sentence,
                size=18,
                key=f"s{index}",
                on_tap=lambda e, i=index: self._run_task(
                    self.start_at, i
                ),
            )
            self._texts.append(text)
        self._highlight(self._active)

        self.text_list = ft.ListView(
            self._texts,
            expand=True,
            spacing=10,
            padding=ft.Padding.symmetric(vertical=8),
        )

        self.play_icon = ft.IconButton(
            ft.Icons.PLAY_ARROW, icon_size=36, on_click=self.toggle_play
        )
        self.slider = ft.Slider(
            min=0,
            max=1,
            value=0,
            expand=True,
            on_change_start=self._seek_start,
            on_change_end=self.seek_slider,
        )
        self.position_label = ft.Text("0:00", size=11)
        self.duration_label = ft.Text("0:00", size=11)
        self.progress_ring = ft.ProgressRing(
            width=14,
            height=14,
            stroke_width=2,
            color=ft.Colors.PRIMARY,
            visible=getattr(self, "_synthesizing", False),
        )
        self.status = ft.Text(
            "Ready" if self.page_audio else "Tap play to synthesize",
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )

        player = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.IconButton(
                                ft.Icons.SKIP_PREVIOUS,
                                tooltip="Previous page",
                                on_click=lambda e: self._run_task(
                                    self.prev_page
                                ),
                            ),
                            self.play_icon,
                            ft.IconButton(
                                ft.Icons.SKIP_NEXT,
                                tooltip="Next page",
                                on_click=lambda e: self._run_task(
                                    self.next_page
                                ),
                            ),
                            ft.Text(self.voice, size=11),
                            ft.Text(f"{self.speed:.2f}x", size=11),
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=4,
                    ),
                    ft.Row(
                        [self.position_label, self.slider, self.duration_label],
                        spacing=8,
                    ),
                    ft.Row(
                        [self.progress_ring, self.status],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=6,
                        tight=True,
                    ),
                ],
                spacing=0,
                tight=True,
            ),
        )

        chapter = self.chapters[self.chapter_idx]
        header = ft.Row(
            [
                ft.IconButton(
                    ft.Icons.ARROW_BACK,
                    tooltip="Back",
                    on_click=self.go_back,
                ),
                ft.Column(
                    [
                        ft.Text(
                            self.book["title"],
                            size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        ft.Text(
                            chapter.title,
                            size=16,
                            weight=ft.FontWeight.BOLD,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                    ],
                    expand=True,
                    spacing=0,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                ft.Text(
                    f"{self.page_idx + 1}/{len(self.pages)}",
                    size=11,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            ],
            spacing=4,
        )

        self.content.content = ft.Column(
            [
                ft.Container(
                    content=header,
                    padding=ft.Padding.symmetric(horizontal=4, vertical=4),
                ),
                ft.Divider(height=1),
                ft.Container(
                    content=self.text_list,
                    expand=True,
                    padding=ft.Padding.symmetric(horizontal=16),
                ),
                player,
            ],
            expand=True,
            spacing=0,
        )
        self._update_player()
        self._safe_update()

    # playback ---------------------------------------------------------------

    async def toggle_play(self, e=None) -> None:
        log(
            f"toggle_play state={self._state} busy={self._busy} "
            f"cached={self.page_audio is not None}"
        )
        if self._state == AudioState.PLAYING:
            await self.audio.pause()
            return

        if self._state == AudioState.COMPLETED:
            # The auto-advance task is normally already doing this; if it
            # stalled (e.g. a pause RPC hung), pressing play should still move
            # to the next page instead of replaying the finished one.
            self._advance_after_completion()
            return

        if self._state == AudioState.PAUSED and not self._new_page:
            await self.audio.resume()
            return

        if self._busy:
            # Rendering already in progress: remember the intent.
            self._wants_play = True
            return

        await self._play_page(self._active)

    async def _play_page(self, sentence_index: int) -> None:
        if self._closing:
            return
        if self.page_audio is None:
            await self._prepare_page()
        if self._closing or self.page_audio is None:
            return

        self._active = max(0, min(sentence_index, len(self._texts) - 1))
        self._highlight(self._active)
        start = self.page_audio.timings[self._active].start

        key = (
            self.book["id"],
            self.chapter_idx,
            self.page_idx,
            self.voice,
            self.speed,
        )
        self._new_page = False

        if key == self._audio_key and self.audio is not None:
            # Source is already on the player; play immediately.
            log(f"play existing source at {start:.2f}s")
            await self._start_playback(start)
        else:
            self.status.value = "Preparing audio…"
            self._safe_update()

            wav_file = self.cache.wav_path(
                self.book["id"],
                self.chapter_idx,
                self.page_idx,
                self.voice,
                self.speed,
            )
            if wav_file.exists():
                # audioplayers_linux feeds src straight into GStreamer's
                # playbin "uri", which requires a real URI.
                if getattr(self.page, "platform", None) == ft.PagePlatform.LINUX:
                    src = wav_file.as_uri()
                else:
                    src = str(wav_file)
            else:
                src = self.page_audio.wav
            log(f"set src: {src if isinstance(src, str) else '<bytes>'}")

            await self._swap_audio(src)
            if self._closing or self.audio is None:
                return

            ready = False
            try:
                await asyncio.wait_for(self._audio_loaded.wait(), timeout=1.5)
                ready = True
            except asyncio.TimeoutError:
                # Some Linux/GStreamer builds do not deliver on_loaded for
                # local files; fall back to asking for the duration.
                for _ in range(3):
                    try:
                        duration = await self.audio.get_duration()
                    except Exception:
                        duration = None
                    if duration is not None and duration.in_milliseconds > 0:
                        ready = True
                        break
                    await asyncio.sleep(0.15)
            log(f"source ready={ready}")

            self._audio_key = key
            self.status.value = "Ready" if ready else "Audio not ready"
            self._safe_update()
            await self._start_playback(start)

        self._run_task(self._prefetch_next)

    async def _swap_audio(self, src) -> None:
        if self._closing:
            return
        old = self.audio
        if old is not None:
            try:
                await asyncio.wait_for(old.pause(), timeout=1.0)
                await asyncio.wait_for(old.release(), timeout=1.0)
            except Exception:
                pass
            if old in self.page.services:
                self.page.services.remove(old)

        self._audio_loaded = asyncio.Event()
        try:
            self.audio = Audio(
                src=src,
                release_mode=ReleaseMode.STOP,
                on_position_change=self._on_position,
                on_state_change=self._on_state,
                on_loaded=self._on_audio_loaded,
            )
        except RuntimeError:
            # Registering the service updates the page; it fails if the
            # session was destroyed while the page was being prepared.
            self._closing = True
            self.audio = None
            return
        self._state = AudioState.STOPPED
        self.page.services.append(self.audio)
        self._safe_update()

        self._run_task(self._prefetch_next)

    async def _start_playback(self, start: float) -> None:
        if self._closing or self.audio is None:
            return
        try:
            # Seeking a freshly-loaded, stopped player can hang on Linux;
            # only seek when actually starting mid-page.
            if start > 0 or self._state == AudioState.COMPLETED:
                await asyncio.wait_for(
                    self.audio.play(position=ft.Duration(seconds=start)),
                    timeout=3.0,
                )
            else:
                await asyncio.wait_for(self.audio.resume(), timeout=3.0)

            # If the source was not ready yet, nudge it once.
            await asyncio.sleep(0.6)
            if self._state != AudioState.PLAYING:
                log("playback did not start; retrying with play()")
                await asyncio.wait_for(
                    self.audio.play(position=ft.Duration(seconds=start)),
                    timeout=3.0,
                )

            if not self._polling:
                self._polling = True
                self._run_task(self._poll_position)
        except Exception:
            traceback.print_exc()
            self.status.value = "Audio error"
            self._safe_update()

    async def start_at(self, index: int) -> None:
        if self.audio is not None and self._state == AudioState.PLAYING:
            try:
                await asyncio.wait_for(self.audio.pause(), timeout=1.0)
            except Exception:
                pass
        await self._play_page(index)

    async def _prepare_page(self) -> None:
        if self._busy or self.book is None or self._closing:
            return

        book_id = self.book["id"]
        chapter_idx = self.chapter_idx
        page_idx = self.page_idx
        voice = self.voice
        speed = self.speed
        sentences = self.pages[page_idx] if page_idx < len(self.pages) else []
        if not sentences:
            return

        cached = self.cache.load(book_id, chapter_idx, page_idx, voice, speed)
        if cached:
            log("page cache hit")
            if self._is_current(chapter_idx, page_idx, voice, speed):
                self.page_audio = cached
                self._set_synthesizing(False)
                self._update_player()
            self._run_task(self._prefetch_next)
            return

        log("page cache miss")
        self._busy = True
        self._set_synthesizing(True)
        self.status.value = (
            "Loading model…" if self.tts is None else "Synthesizing…"
        )
        self._safe_update()

        def still_current() -> bool:
            return self._is_current(chapter_idx, page_idx, voice, speed)

        def progress(index: int, total: int) -> None:
            self.status.value = f"Synthesizing {index}/{total}…"
            self._safe_update()

        try:
            prefetch = self._prefetch_tasks.get(
                (book_id, chapter_idx, page_idx, voice, speed)
            )
            if prefetch is not None and not prefetch.done():
                log("waiting for prefetch to finish this page")
                try:
                    await prefetch
                except Exception:
                    pass

            audio = await self._render_sentences(
                sentences,
                voice=voice,
                speed=speed,
                keep_going=still_current,
                on_progress=progress,
                cache_check=lambda: self.cache.load(
                    book_id, chapter_idx, page_idx, voice, speed
                ),
            )
            if audio is None:
                log("page changed during synthesis; result discarded")
            elif self._closing:
                log("session closed during synthesis; result discarded")
            else:
                self.cache.save(
                    book_id,
                    chapter_idx,
                    page_idx,
                    voice,
                    speed,
                    audio,
                )
                if still_current():
                    self.page_audio = audio
                    self.status.value = "Ready"
                else:
                    log("page changed during synthesis; result discarded")
        except Exception as exc:
            traceback.print_exc()
            self._wants_play = False
            self.status.value = f"TTS error: {exc}"
            self._snack(f"TTS error: {exc}")
        finally:
            self._busy = False
            self._set_synthesizing(False)
            self._update_player()

        if self._closing:
            return

        if not self._is_current(chapter_idx, page_idx, voice, speed):
            # The user moved on while this page was rendering.
            self._run_task(self._prepare_page)
        elif self._wants_play and self.page_audio is not None:
            self._wants_play = False
            await self._play_page(self._active)

        self._run_task(self._prefetch_next)

    def _is_current(
        self, chapter_idx: int, page_idx: int, voice: str, speed: float
    ) -> bool:
        return (
            self.book is not None
            and self.chapter_idx == chapter_idx
            and self.page_idx == page_idx
            and self.voice == voice
            and self.speed == speed
        )

    async def _synthesize_one(
        self,
        sentence: str,
        *,
        tts,
        voice: str,
        speed: float,
        keep_going=None,
        prefetch: bool = False,
    ):
        """Synthesize a single sentence, respecting the shared thread slots.

        Prefetch work is limited to one slot fewer than the total, keeping a
        slot free for a page the reader is waiting on.
        """

        async def run():
            async with self._synth_slots:
                if self._closing or (
                    keep_going is not None and not keep_going()
                ):
                    return None
                return await asyncio.to_thread(
                    tts.synthesize,
                    sentence,
                    voice=voice,
                    speed=speed,
                )

        if prefetch:
            async with self._prefetch_slots:
                return await run()
        return await run()

    async def _render_sentences(
        self,
        sentences: list[str],
        *,
        voice: str,
        speed: float,
        keep_going=None,
        on_progress=None,
        cache_check=None,
        on_rendered=None,
    ) -> PageAudio | None:
        """Synthesize one page for the reader."""
        if not sentences:
            return assemble([], sentences)
        if self._closing:
            return None
        if keep_going is not None and not keep_going():
            return None
        if cache_check is not None:
            cached = cache_check()
            if cached is not None:
                log("prefetch already rendered this page")
                return cached

        tts = await self._ensure_model(voice)
        completed = 0

        async def one(sentence: str):
            nonlocal completed
            part = await self._synthesize_one(
                sentence,
                tts=tts,
                voice=voice,
                speed=speed,
                keep_going=keep_going,
            )
            if part is not None:
                completed += 1
                if on_progress is not None:
                    on_progress(completed, len(sentences))
            return part

        parts = await asyncio.gather(*(one(sentence) for sentence in sentences))
        if any(part is None for part in parts):
            return None

        audio = assemble(parts, sentences)
        if on_rendered is not None:
            on_rendered(audio)

        if keep_going is not None and not keep_going():
            log("completed page cached even though it is no longer needed")
            return None
        return audio

    def _load_tts(self, voice: str | None = None):
        from soundbook.core.tts.onnx_backend import OnnxTTS

        voice = voice or self.voice
        language = "en-gb" if voice.startswith("b") else "en-us"

        with _TTS_MODELS_LOCK:
            cached = _TTS_MODELS.get(language)
            if cached is not None:
                log(f"reusing preloaded {language} model")
                return cached

            tts = OnnxTTS(
                model_path=config.model_path(),
                voices_path=config.voices_path(),
                g2p_dir=config.g2p_dir(),
                language=language,
            )
            _TTS_MODELS[language] = tts
            return tts

    def _upcoming_pages(
        self, count: int
    ) -> list[tuple[int, int, list[str]]]:
        """Up to `count` pages after the current one, across chapters."""
        upcoming: list[tuple[int, int, list[str]]] = []
        chapter_idx = self.chapter_idx
        page_idx = self.page_idx
        pages = self.pages

        while len(upcoming) < count:
            page_idx += 1
            if page_idx >= len(pages):
                chapter_idx += 1
                page_idx = 0
                if chapter_idx >= len(self.chapters):
                    break
                pages = paginate(split_sentences(self.chapters[chapter_idx].text))
                if not pages:
                    break
            upcoming.append((chapter_idx, page_idx, pages[page_idx]))

        return upcoming

    def _prefetch_active(
        self, book_id: int, voice: str, speed: float
    ) -> list[asyncio.Task]:
        return [
            task
            for key, task in self._prefetch_tasks.items()
            if key[0] == book_id
            and key[3] == voice
            and key[4] == speed
            and not task.done()
        ]

    async def _prefetch_page(
        self,
        book_id: int,
        chapter_idx: int,
        page_idx: int,
        sentences: list[str],
        voice: str,
        speed: float,
    ) -> None:
        """Render and cache one page; other prefetched pages run alongside."""
        key = (book_id, chapter_idx, page_idx, voice, speed)
        try:
            if self._closing or not sentences:
                return
            tts = await self._ensure_model(voice)

            def keep_going() -> bool:
                if self._closing or self.book is None:
                    return False
                if self.book["id"] != book_id:
                    return False
                if self.voice != voice or self.speed != speed:
                    return False
                if self._view != "reader":
                    return False
                current = (self.chapter_idx, self.page_idx)
                if (chapter_idx, page_idx) == current:
                    # The listener reached this page: finish and cache it.
                    return True
                return not self._busy and (chapter_idx, page_idx) > current

            async def one(sentence: str):
                return await self._synthesize_one(
                    sentence,
                    tts=tts,
                    voice=voice,
                    speed=speed,
                    keep_going=keep_going,
                    prefetch=True,
                )

            parts = await asyncio.gather(
                *(one(sentence) for sentence in sentences)
            )
            if any(part is None for part in parts):
                return

            audio = assemble(parts, sentences)
            self.cache.save(
                book_id, chapter_idx, page_idx, voice, speed, audio
            )
            log(f"prefetched chapter {chapter_idx} page {page_idx}")
        except Exception:
            traceback.print_exc()
        finally:
            self._prefetch_tasks.pop(key, None)

    async def _prefetch_next(self) -> None:
        """Keep up to `PREFETCH_PAGES` pages rendering in the background.

        Pages save independently as soon as their own sentences finish, so the
        next page never waits for a later page's long sentence.
        """
        if (
            self._prefetching
            or self.book is None
            or self._view != "reader"
            or self._closing
        ):
            return

        book_id = self.book["id"]
        voice = self.voice
        speed = self.speed
        self._prefetching = True
        try:
            while True:
                if self._closing or self.book is None:
                    break
                if self.book["id"] != book_id:
                    break
                if self.voice != voice or self.speed != speed:
                    break
                if self._view != "reader":
                    break
                if self._busy:
                    # A page the user is waiting for takes priority.
                    break

                active = self._prefetch_active(book_id, voice, speed)
                if len(active) < PREFETCH_PAGES:
                    for chapter_idx, page_idx, sentences in self._upcoming_pages(
                        PREFETCH_AHEAD
                    ):
                        if len(active) >= PREFETCH_PAGES:
                            break
                        if not sentences:
                            continue
                        if self.cache.has(
                            book_id, chapter_idx, page_idx, voice, speed
                        ):
                            continue
                        key = (book_id, chapter_idx, page_idx, voice, speed)
                        existing = self._prefetch_tasks.get(key)
                        if existing is not None and not existing.done():
                            continue

                        log(
                            f"prefetch chapter {chapter_idx} "
                            f"page {page_idx}"
                        )
                        task = asyncio.create_task(
                            self._prefetch_page(
                                book_id,
                                chapter_idx,
                                page_idx,
                                sentences,
                                voice,
                                speed,
                            )
                        )
                        self._prefetch_tasks[key] = task
                        active.append(task)

                if not active:
                    break

                # Wait for one page to finish, then refill the pipeline while
                # the remaining pages keep rendering.
                await asyncio.wait(
                    active, return_when=asyncio.FIRST_COMPLETED
                )
        except Exception:
            traceback.print_exc()
        finally:
            self._prefetching = False

    def _advance_after_completion(self) -> None:
        if self._advancing or self._closing:
            return
        self._advancing = True
        self._run_task(self._advance)

    async def _advance(self) -> None:
        try:
            await self.next_page()
        finally:
            self._advancing = False

    async def next_page(self, e=None) -> None:
        if self._closing:
            return
        if self.page_idx + 1 < len(self.pages):
            await self._goto(self.chapter_idx, self.page_idx + 1)
        elif self.chapter_idx + 1 < len(self.chapters):
            await self._goto(self.chapter_idx + 1, 0)
        else:
            sentences = (
                self.pages[self.page_idx]
                if self.page_idx < len(self.pages)
                else []
            )
            self._save_progress(max(0, len(sentences) - 1), done=True)
            self.status.value = "Finished"
            self._safe_update()

    async def prev_page(self, e=None) -> None:
        if self.page_idx > 0:
            await self._goto(self.chapter_idx, self.page_idx - 1)
        elif self.chapter_idx > 0:
            await self._goto(self.chapter_idx - 1, 10**6)

    async def _goto(self, chapter_idx: int, page_idx: int) -> None:
        if self._closing:
            return
        log(f"advance to chapter {chapter_idx} page {page_idx}")
        # Pausing a player that already reached COMPLETED can hang on some
        # GStreamer builds. The old source is released by _swap_audio anyway.
        if self.audio is not None and self._state == AudioState.PLAYING:
            try:
                await asyncio.wait_for(self.audio.pause(), timeout=1.0)
            except Exception:
                pass
        if self._closing:
            return
        self.page_audio = None
        self.load_page(chapter_idx, page_idx)
        if self._closing:
            return
        await self._play_page(0)

    def _on_audio_loaded(self, e=None) -> None:
        self._audio_loaded.set()

    def _on_state(self, e) -> None:
        if self._closing:
            return
        log(f"state={e.state}")
        self._state = e.state
        if hasattr(self, "play_icon"):
            self.play_icon.icon = (
                ft.Icons.PAUSE
                if e.state == AudioState.PLAYING
                else ft.Icons.PLAY_ARROW
            )
            self._safe_update()

        if e.state == AudioState.COMPLETED and self._view == "reader":
            log("page complete; advancing")
            self._advance_after_completion()

    def _on_position(self, e) -> None:
        self._apply_position(e.position)

    def _apply_position(self, milliseconds: int) -> None:
        if self._closing or self.page_audio is None or self._seeking:
            return

        seconds = milliseconds / 1000
        self.slider.value = min(seconds, self.page_audio.duration)
        self.position_label.value = fmt_time(seconds)

        index = self.page_audio.sentence_at(seconds)
        if index != self._active:
            self._active = index
            self._highlight(index)
            self._save_progress(index)
            self._run_task(self._scroll_to, index)
        self._safe_update()

    def _save_progress(self, sentence_idx: int, done: bool = False) -> None:
        """Persist the reading position; `done` marks the book finished."""
        if self.transient or self.book is None:
            return

        sentences = (
            self.pages[self.page_idx] if self.page_idx < len(self.pages) else []
        )
        if done:
            page_progress = 1.0
        else:
            page_progress = sentence_idx / max(1, len(sentences))

        self.store.save_progress(
            self.book["id"],
            self.chapter_idx,
            self.page_idx,
            sentence_idx,
            page_progress,
        )

    async def _poll_position(self) -> None:
        """Fallback for clients that do not deliver position events."""
        ticks = 0
        while (
            not self._closing
            and self._state == AudioState.PLAYING
            and self.audio is not None
        ):
            try:
                position = await self.audio.get_current_position()
            except Exception:
                position = None
            if position is not None:
                ticks += 1
                if ticks % 5 == 1:
                    log(f"position {position.in_milliseconds} ms")
                self._apply_position(position.in_milliseconds)
            await asyncio.sleep(1)
        self._polling = False

    def _seek_start(self, e) -> None:
        self._seeking = True

    async def seek_slider(self, e) -> None:
        self._seeking = False
        if self.page_audio is not None and self.audio is not None:
            await self.audio.seek(ft.Duration(seconds=float(e.control.value)))

    def _highlight(self, index: int) -> None:
        for position, text in enumerate(self._texts):
            text.bgcolor = HIGHLIGHT if position == index else None

    async def _scroll_to(self, index: int) -> None:
        try:
            await self.text_list.scroll_to(
                scroll_key=f"s{index}", duration=200
            )
        except Exception:
            pass

    def _set_synthesizing(self, active: bool) -> None:
        """Toggle the progress ring shown while a page is being prepared."""
        self._synthesizing = active
        ring = getattr(self, "progress_ring", None)
        if ring is not None:
            ring.visible = active

    def _update_player(self) -> None:
        if not hasattr(self, "play_icon"):
            return
        if self.page_audio is not None:
            self.slider.max = max(0.001, self.page_audio.duration)
            self.duration_label.value = fmt_time(self.page_audio.duration)
        self.play_icon.icon = (
            ft.Icons.PAUSE
            if self._state == AudioState.PLAYING
            else ft.Icons.PLAY_ARROW
        )
        self._safe_update()

    # settings ---------------------------------------------------------------

    def show_settings(self, e=None) -> None:
        self._view = "settings"
        self.nav.selected_index = 2
        voices = [
            voice
            for voice in list_voices(config.voices_path())
            if voice[0] in SPEAKABLE
        ]

        self.voice_dropdown = ft.Dropdown(
            label="Voice",
            value=self.voice,
            options=[
                ft.DropdownOption(
                    key=voice,
                    text=f"{voice}  ·  {LANGUAGES.get(voice[0], '?')}",
                )
                for voice in voices
            ],
            on_select=self._voice_selected,
        )
        self.speed_slider = ft.Slider(
            min=0.5,
            max=2.0,
            divisions=15,
            value=self.speed,
            label="{value:.2f}x",
            on_change_end=self._speed_selected,
        )

        cache_mb = self.cache.size_bytes() / 1_000_000
        self.content.content = ft.Container(
            expand=True,
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            content=ft.ListView(
                [
                    ft.Text("Settings", size=26, weight=ft.FontWeight.BOLD),
                    ft.Text("Voice", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                    self.voice_dropdown,
                    ft.Text(
                        "Speed", size=12, color=ft.Colors.ON_SURFACE_VARIANT
                    ),
                    ft.Row(
                        [
                            ft.Text(f"{self.speed:.2f}x", width=50),
                            self.speed_slider,
                        ]
                    ),
                    ft.Divider(),
                    ft.Text(
                        f"Cached audio: {cache_mb:.1f} MB",
                        size=12,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    ft.TextButton(
                        "Clear cache", on_click=self._clear_cache
                    ),
                    ft.Divider(),
                    ft.Text(
                        "Kokoro v1.0 int8 · 54 voices · "
                        "2 G2P languages (English US/UK)",
                        size=11,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                ],
                spacing=10,
            ),
        )
        self._safe_update()

    def _voice_selected(self, e) -> None:
        self.voice = e.control.value
        self.store.set_setting("voice", self.voice)
        self.tts = None
        self.page_audio = None
        self._snack("Voice saved. New pages will use it.")

    def _speed_selected(self, e) -> None:
        self.speed = float(e.control.value)
        self.store.set_setting("speed", f"{self.speed:.2f}")
        self.page_audio = None
        self._snack("Speed saved. New pages will use it.")

    def _clear_cache(self, e=None) -> None:
        freed = self.cache.clear()
        self._snack(f"Freed {freed / 1_000_000:.1f} MB")
        self.show_settings()


def main(page: ft.Page) -> None:
    SoundbookApp(page)