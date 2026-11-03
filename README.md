# Soundbook

Offline reader with Kokoro-82M text-to-speech, built with Python + Flet.

- Android, Linux, Windows (on-device ONNX inference)
- Web (planned: server-backed; Pyodide has no `onnxruntime`)
- EPUB, PDF, TXT, Markdown and HTML import (Markdown headings become chapters)
- **Web link** import: fetch a URL, keep the main article, skip page chrome
- **Type** tab for transcribing a pasted paragraph immediately
- Sentence highlighting, page-level audio, disk cache, voice + speed settings

## Requirements

- Python 3.12
- `uv`

## Assets

The model and dictionaries are ~129 MB and are **not** committed:

```
src/assets/kokoro/kokoro-v1.0.int8.onnx   # 89 MB
src/assets/kokoro/voices-v1.0.bin         # 27 MB, 54 voices
src/assets/g2p/us_gold.json               # 2.9 MB
src/assets/g2p/us_silver.json             # 3.0 MB
src/assets/g2p/gb_gold.json               # 2.8 MB
src/assets/g2p/gb_silver.json             # 3.5 MB
```

Model: <https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0>
Dictionaries: `misaki` 0.9.4 wheel, `misaki/data/*.json`.

## Run

```bash
uv sync
uv run flet run            # desktop
uv run flet run --web      # browser (TTS backend not wired yet)
```

Then tap **+** and import an `.epub`, `.pdf`, `.txt`, `.md` or `.html` file,
tap the link button to read a web article, or open the **Type** tab and paste
text directly.

Synthesis happens on first play; the page is cached under the app cache dir, so
replaying a page is instant. The TTS model is preloaded in the background right
after the library appears, so opening a book does not pay the model-load cost.
While a page is playing, the next pages are synthesized in the background (up
to four ahead), and the sentences of each page are rendered in parallel, so
playback normally continues without waiting when the current page ends.

Reader pages hold about 720 characters so a page usually contains several
sentences for the parallel synthesizer. Upgrading from an older version
repaginates existing books once and clears the rendered-audio cache, because
old page numbers no longer match the new text.

## HTML files and web links

HTML import strips scripts, navigation, headers, footers, sidebars and forms,
then keeps the densest article container. Web links use the same extractor:
the app downloads the page (5 MB limit, HTML only) and follows redirects.
`<h2>` headings become chapters; pages that render their text only with
JavaScript, or that sit behind a login/paywall, cannot be imported.

## PDFs: preprocess long or complex files

A PDF is a layout format, so a reader app can only guess at reading order.
Soundbook's built-in PDF import is a best-effort fallback: it drops the
top/bottom margin bands, removes lines repeated on most pages, skips flattened
table rows, repairs hyphenated line breaks and uses the PDF outline for chapter
titles when one exists.

For long reports, two-column layouts, tables or charts, run the stand-alone
preprocessor first and import the resulting Markdown. It uses `pymupdf4llm` for
layout-aware extraction, then removes running headers/footers, margin text,
chart labels and tables:

```bash
uv run --with pymupdf4llm python scripts/pdf2text.py report.pdf -o report.md
```

Then import `report.md` in Soundbook. Every heading becomes a chapter, and the
generated file carries the PDF title/author as front matter. Pass
`--keep-tables` to keep Markdown tables, or `--margin 0.08` to trim a wider
top/bottom band. Scanned PDFs without a text layer are not OCR'd.

## Test

```bash
uv run pytest -q
```

51 tests cover tokenization, dictionary G2P, sentence/page splitting, audio
assembly and timing, parsing (EPUB/PDF/Markdown/HTML/TXT), PDF layout clean-up,
web extraction, sqlite storage, cache round-trips and the progress/migration
logic.

## Build

```bash
uv run flet build apk --split-per-abi
uv run flet build linux
uv run flet build windows
```

The APK is ~160–175 MB per ABI because the model ships inside the app.

## Known gaps

- **Background/lock-screen audio** is not implemented. `flet-audio` wraps
  `audioplayers` without `audio_service`; a custom Flet extension (plus a
  modified `MainActivity` and manifest entries) is required.
- **Web TTS** needs the FastAPI + PyTorch server backend. Until then the web
  build shows a TTS error on play.
- **Languages**: only English US/UK have bundled G2P. Chinese needs
  `jieba` + `pypinyin`; other languages need espeak or the server.
- Word-level highlighting is sentence-level (Kokoro gives no word timings).
- Numbers and abbreviations are not normalized before G2P yet.
- Built-in PDF import uses one chapter per page when the PDF has no outline;
  run `scripts/pdf2text.py` for real chapter detection from headings.
- Scanned PDFs need OCR before import (`scripts/pdf2text.py` does not OCR).
- Web import is a readability-style heuristic, not a browser: JavaScript-only
  pages, paywalls and cookie walls are not handled. Re-importing the same URL
  reuses the stored copy instead of refreshing it.

## Layout

```
src/main.py                     Flet entry point
src/soundbook/app.py            UI, playback, import
src/soundbook/config.py         asset/data/cache paths
src/soundbook/core/document.py  EPUB / PDF / Markdown / HTML / TXT parsing
src/soundbook/core/webpage.py  HTML article extraction + web link fetching
src/soundbook/core/pdf_clean.py PDF margin, header/footer and table clean-up
src/soundbook/core/chunker.py   sentences -> pages
src/soundbook/core/audio.py     assembly, timings, WAV, cache
src/soundbook/core/store.py     sqlite library + progress
src/soundbook/core/tts/         vocabulary, G2P, ONNX backend
scripts/synth.py                CLI: text -> WAV
scripts/pdf2text.py             CLI: PDF -> clean Markdown for import
```