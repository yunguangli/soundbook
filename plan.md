# Soundbook — Implementation Plan

A phone-friendly book/essay reader with on-device Kokoro-82M text-to-speech, built with Python + Flet.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Platforms | Android (primary), Linux, Windows, Web |
| GUI | Flet 1.0.x |
| TTS model | [hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) via **ONNX** (int8) |
| Runtime (Android/desktop) | `onnxruntime` |
| Runtime (web) | Server-backed: FastAPI + `torch` + `kokoro` |
| Content sources | EPUB + PDF + plain text |
| Model delivery | Bundled in APK assets |
| Background / lock-screen audio | **Must-have** — custom Flet extension |
| G2P | Bundled dictionary lookup (misaki JSON) + OOV fallback |

## Why ONNX, not PyTorch

Flet's mobile wheel index (`pypi.flet.dev`) has **no `torch`** and **no `transformers`**,
so neither `kokoro` nor `misaki` can run inside an Android build.

Verified available on `pypi.flet.dev`:

| Package | Android | iOS | Notes |
|---|---|---|---|
| `onnxruntime` 1.27.0 | ✅ arm64, x86_64 | ✅ arm64 | cp312/313/314 |
| `numpy` | ✅ | ✅ | 1.26.4 / 2.2.2 / 2.4.6 |
| `scipy` | ✅ | ✅ | |
| `safetensors`, `soundfile` | ✅ | ✅ | |
| `PyMuPDF` 1.27.2.3 | ✅ | ✅ | PDF/EPUB extraction |
| `torch`, `transformers`, `phonemizer` | ❌ | ❌ | blockers for `kokoro` / `misaki` |

`misaki/en.py` imports `torch` and `transformers` at module level, so even the G2P
frontend is unusable on-device. Do **not** install `kokoro-onnx` either: it pulls
`phonemizer` + `espeakng-loader`, and that wheel only ships a manylinux
`libespeak-ng.so` (nothing loadable on Android).

### Why not ncnn or llama.cpp

- **llama.cpp** — upstream `llama-tts` supports OuteTTS / Qwen3-TTS / PocketTTS /
  Soprano. Kokoro support (issue #11050) was closed unmerged. Kokoro GGUF exists
  only in a separate project (`mmwillet/TTS.cpp`), not in `llama_cpp_python`, and
  is not packaged for Flet.
- **ncnn** — Kokoro is StyleTTS2 + iSTFTNet: 6 bidirectional LSTMs, AdaIN
  InstanceNorm, complex STFT/iSTFT, data-dependent output length. `onnx2ncnn`
  won't convert that cleanly, and there is no known Kokoro ncnn port. Weeks of
  custom-layer work for no size or speed win over a tested ONNX export.
- `llama_cpp_python` on `pypi.flet.dev` is still useful later for a small LLM
  (summaries, dictionary lookup) — just not for TTS.

## Platform matrix

| Target | Python runtime | TTS backend | Document parser |
|---|---|---|---|
| `flet build apk` | bundled CPython 3.12 | `OnnxBackend` (int8) | PyMuPDF |
| `flet build linux` / `windows` | bundled CPython | `OnnxBackend` (or full torch) | PyMuPDF |
| `flet build web` | Pyodide | ❌ no ORT → `HttpBackend` | pypdf + ebooklib |
| `flet run --web` | CPython on server | full `torch` + `kokoro` | PyMuPDF |

Pyodide ships `numpy`, `scipy`, `soundfile`, `safetensors`, `lxml` — but **not
`onnxruntime` and not `PyMuPDF`**.

Architecture:

```
Reader UI (Flet)
 ├─ Document parsers
 ├─ Chunker (chapter → page → sentence)
 ├─ Progress store (sqlite)
 ├─ TTSBackend (Protocol)
 │    ├─ OnnxBackend   onnxruntime + Kokoro int8   (Android, desktop)
 │    ├─ TorchBackend  kokoro + misaki             (desktop, server)
 │    └─ HttpBackend   POST /audio                  (web)
 └─ Audio queue (page-level, prefetch + cache)
```

## Verified Flet API surface (1.0.5, Flet MCP)

| Symbol | Verified fact | Impact |
|---|---|---|
| `Audio` | service in `flet-audio`; `src: str \| bytes`; async `play/pause/resume/seek/release` | raw WAV bytes or a file path both work |
| `AudioPositionChangeEvent.position` | **milliseconds** | convert to seconds for highlight lookup |
| `ReleaseMode` | `RELEASE`, `LOOP`, `STOP` | use `STOP` so pause/resume keeps resources |
| `StoragePaths` | separate service; `get_application_cache_directory()` etc. | use instead of `FLET_APP_STORAGE_*` env vars |
| `ft.run` | `run(main, ..., assets_dir=...)` | matches scaffold |
| `Page` | **no** `storage_paths` attribute | do not use `page.storage_paths` |
| `PyMuPDF`, `onnxruntime` | not Pyodide packages | must stay per-platform dependencies |

## pyproject.toml changes

The scaffold is the stock Flet 1.0 template. Required changes:

```toml
[project]
name = "soundbook"
version = "0.1.0"
readme = "README.md"
requires-python = ">=3.12,<3.13"
dependencies = [
    "flet==1.0.5",
    "flet-audio==1.0.5",
    "numpy",
    # pure-Python parsers keep the Pyodide (web) build resolvable
    "pypdf",
    "ebooklib",
    "beautifulsoup4",
]

[tool.flet]
desktop_flavor = "full"          # REQUIRED: "light" omits flet-audio
org = "com.mycompany"
product = "soundbook"
company = "Flet"
copyright = "Copyright (C) 2026"

[tool.flet.app]
path = "src"

[tool.flet.android]
split_per_abi = true

# Native-only dependencies: these have no Pyodide wheels, so they must NOT be
# in the base dependency list.
[tool.flet.android.dependencies]
onnxruntime = ">=1.27.0"
pymupdf = ">=1.27"

[tool.flet.linux.dependencies]
onnxruntime = ">=1.27.0"
pymupdf = ">=1.27"

[tool.flet.windows.dependencies]
onnxruntime = ">=1.27.0"
pymupdf = ">=1.27"

# Background playback (manifest side; service/receiver come from the extension)
[tool.flet.android.permission]
"android.permission.WAKE_LOCK" = true
"android.permission.FOREGROUND_SERVICE" = true
"android.permission.FOREGROUND_SERVICE_MEDIA_PLAYBACK" = true
"android.permission.POST_NOTIFICATIONS" = true

# Do not ship ~100 MB of model to the web build (web is server-backed anyway)
[tool.flet.web.app]
exclude = ["assets/models", "assets/g2p"]

# Local Flet extension (path form to confirm against the installed Flet version)
[tool.flet.dev_packages]
flet-audio-service = { path = "../flet_audio_service" }
```

Additional files:

- `.python-version` → `3.12`
- `.gitignore` → add `src/assets/models/` and `src/assets/g2p/`

> `PyMuPDF` must stay out of `[project].dependencies`; `flet build web`
> resolves base dependencies against Pyodide and will fail.

## Project structure

```
soundbook/
├─ plan.md
├─ pyproject.toml
├─ .python-version
├─ src/
│  ├─ main.py                       # thin entry → soundbook.app
│  ├─ assets/
│  │  ├─ icon.png
│  │  ├─ g2p/us_gold.json           # 2.9 MB, bundled (gitignored)
│  │  ├─ g2p/us_silver.json         # 3.0 MB, bundled (gitignored)
│  │  ├─ g2p/gb_gold.json           # 2.8 MB, bundled (gitignored)
│  │  ├─ g2p/gb_silver.json         # 3.5 MB, bundled (gitignored)
│  │  └─ models/
│  │     ├─ kokoro-v1.0.int8.onnx   # ~88 MB, bundled (gitignored)
│  │     └─ voices-v1.0.bin         # ~27 MB, 54 voices (gitignored)
│  └─ soundbook/
│     ├─ app.py                     # shell, routing, lifecycle
│     ├─ config.py                  # FLET_ASSETS_DIR + StoragePaths service
│     ├─ ui/
│     │  ├─ library.py              # book list / import
│     │  ├─ reader.py               # text view + highlight
│     │  └─ player.py               # transport bar
│     └─ core/
│        ├─ document.py             # PyMuPDF (native) | pypdf+ebooklib (web)
│        ├─ chunker.py              # chapter → page → sentence
│        ├─ audio.py                # concat, WAV writer, sentence sample ranges
│        ├─ store.py                # sqlite: library, progress, bookmarks
│        └─ tts/
│           ├─ base.py              # TTSBackend protocol
│           ├─ onnx_backend.py      # ORT + int8 model
│           ├─ torch_backend.py     # desktop/server only
│           ├─ http_backend.py      # web → FastAPI
│           └─ g2p.py               # dictionary G2P + OOV fallback
├─ flet_audio_service/              # custom Flet extension (Dart)
│  ├─ pyproject.toml
│  ├─ src/flet_audio_service/       # Python service API
│  └─ src/flutter/flet_audio_service/
│     ├─ pubspec.yaml               # just_audio, just_audio_background
│     ├─ lib/src/extension.dart
│     └─ android/src/main/AndroidManifest.xml
├─ flet-build-template/             # fork of flet-dev/flet-build-template
├─ server/app.py                    # FastAPI + torch Kokoro (web)
└─ tests/
   ├─ test_chunker.py
   ├─ test_audio.py                 # concat + timing math, no model needed
   └─ test_parser.py
```

## Phases

| # | Work | Gate |
|---|---|---|
| **0a** | ORT int8 benchmark screen, debug APK on the real phone | faster than ~1.5× realtime, else rethink |
| **0b** | Background-audio spike: extension, `just_audio_background`, custom template, lock screen + notification controls | must work before reader work |
| **1** | Library/reader UI, EPUB/PDF/TXT ingest, sentence split, sqlite progress | can navigate a book |
| **2** | `OnnxBackend` + dictionary G2P → real speech on device | book reads aloud offline |
| **3** | Page audio assembly, exact sentence timings, highlight, prefetch, cache eviction | usable reader |
| **4** | Linux/Windows builds | |
| **5** | Web: `HttpBackend` + FastAPI Kokoro server | |

0a and 0b are independent and both block everything else. Build both into one
debug APK and validate on the actual device first.

## GUI

Mobile-first, Material 3, `ft.NavigationBar` at the bottom: **Library / Type /
Settings**. Desktop and web reuse the same components in a two-pane layout
(library sidebar + reader).

### 1. Library

```
┌─────────────────────────────┐
│ Soundbook           [+] [⚙] │
├─────────────────────────────┤
│ ┌───────┐ The Great Gatsby  │
│ │ cover │ F. Scott Fitzgerald│
│ │       │ ▓▓▓▓▓░░░ 42%      │
│ └───────┘ ▶ Continue        │
│ ┌───────┐ Meditations       │
│ │ cover │ Marcus Aurelius   │
│ └───────┘ ▓▓░░░░░░ 18%      │
├─────────────────────────────┤
│  Library  Now Playing  ⚙    │
└─────────────────────────────┘
```

### 2. Reader

Active sentence highlighted; tap any sentence to seek there.

```
┌─────────────────────────────┐
│ ←  Chapter 3          ☰  Aa │
├─────────────────────────────┤
│ In my younger and more      │
│ vulnerable years my father  │
│ gave me some advice that    │
│ █I've been turning over in █│
│ my mind ever since.         │
│                             │
│ "Whenever you feel like     │
│ criticizing any one," he    │
│ told me...                  │
├─────────────────────────────┤
│ ⏮  ⏸  1.25×  ─────●────    │
└─────────────────────────────┘
```

### 3. Now Playing

```
┌─────────────────────────────┐
│             ⋮               │
│       ┌───────────┐         │
│       │   cover   │         │
│       └───────────┘         │
│       The Great Gatsby      │
│       Chapter 3 · 18:30     │
│   ──────●────────────────   │
│   04:21            18:30    │
│        ⏮   ⏸   ⏭          │
│   Voice  Speed  Sleep  ☰    │
└─────────────────────────────┘
```

### 4. Import

`FilePicker` (EPUB/PDF/TXT) → parsing progress → chapter checklist.

### 5. Settings

```
Voice     af_heart ▾   [▶ preview]
Speed     ─────●────  1.00×
Cache     182 MB / 300 MB  [Clear]
Keep playing when locked   [on]
```

### 6. Type

Multiline text field with a character count and a **Read** button. Typed text is
transient: it opens the reader immediately, is not added to the library, and the
draft is persisted in settings so it survives tab switches and restarts.

### Voices and languages

`voices-v1.0.bin` contains **54 voices across 9 languages**. The v1.0 weights are
shared; the language is selected by the voice prefix, not by a separate model.
The blocker for most languages is **G2P**, not the model.

| Prefix | Language | Voices | G2P status on device |
|---|---|---|---|
| `af_` / `am_` | English (US) | 20 | ✅ dictionary G2P |
| `bf_` / `bm_` | English (UK) | 8 | ✅ dictionary G2P |
| `ef_` / `em_` | Spanish | 3 | ⚠ requires espeak or server |
| `ff_` | French | 1 | ⚠ requires espeak or server |
| `hf_` / `hm_` | Hindi | 4 | ⚠ requires espeak or server |
| `if_` / `im_` | Italian | 2 | ⚠ requires espeak or server |
| `jf_` / `jm_` | Japanese | 5 | ❌ native deps unavailable on Flet |
| `pf_` / `pm_` | Portuguese (BR) | 3 | ⚠ requires espeak or server |
| `zf_` / `zm_` | Chinese (Mandarin) | 8 | ✅ jieba + pypinyin (pure Python) |

UI behaviour:

- Voice picker is a full-screen sheet: language tabs, then voice rows.
- Each row has a `▶` 3-second preview, synthesized once and cached.
- Unsupported languages show a "Server voice" badge and are disabled offline.
- Voice + speed are per-book overrides with a global default in Settings.
- Switching voice applies to the **next page**; already-cached pages keep the
  voice they were generated with.

## Key designs

### ONNX inference (no `kokoro-onnx`)

```python
# core/tts/onnx_backend.py  (~80 lines)
import numpy as np
import onnxruntime as ort

sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
# v1.0 export names the token input "tokens"; newer exports use "input_ids".
id_name = (
    "tokens"
    if "tokens" in {i.name for i in sess.get_inputs()}
    else "input_ids"
)
voices = np.load(voices_path)   # name -> float32 [510, 1, 256]

def synth(ids: list[int], voice: str, speed: float = 1.0) -> np.ndarray:
    feed = {
        id_name: np.array([ids], dtype=np.int64),
        "style": np.asarray(voices[voice][len(ids)], dtype=np.float32).reshape(1, -1),
        "speed": np.array([speed], dtype=np.float32),
    }
    return sess.run(None, feed)[0]
```

Verified against `kokoro-v1.0.int8.onnx`:

- Input names are **`tokens`, `style`, `speed`**.
- Token ids are `[0, ...phonemes..., 0]`; `style` is **one row indexed by the
  token count**, shape `(1, 256)`.
- Voice arrays are `(510, 1, 256)`, so keep chunks under 508 phoneme tokens.
- Output is float32 PCM at 24 kHz.

Read `session.get_inputs()` at startup rather than hardcoding names — exports
differ.

### G2P (the one genuinely unsolved piece)

Kokoro needs IPA tokens, not text. Options considered:

1. **Dictionary G2P (chosen)** — ship misaki's `us_*` + `gb_*` JSON files
   (~13 MB of pure data, no `torch` import) and do word lookup plus a
   fallback. Same approach used by `xybrid`'s mobile Kokoro package.
   Weakness: heteronyms (`read`, `lead`, `bass`).

   Verified quirks of the dictionary files:
   - `us_gold.json` has 790 entries whose value is a **POS-keyed dict**
     (`{"DEFAULT": "..."}`), not a string. Use `DEFAULT` when no tagger exists.
   - Coverage is ~93k words but not complete: `families` is missing while
     `family` is present. A morphological fallback (`-ies`→`-y`, `-es`, `-s`,
     `-ed`, `-ing`) resolves most OOV words; anything left is skipped.
   - Silver first, gold overrides.
2. Pre-compute phonemes on desktop for static books — perfect quality, but only
   for bundled content.
3. Build `libespeak-ng.so` with the NDK — best fidelity, most work, and
   espeak-ng is **GPLv3** (distribution implications).
4. Hybrid: server returns IPA for OOV words only, cached on device.

### Page-level audio assembly

Per page, not per sentence (avoids gaps between utterances):

1. Split the page into sentences.
2. Synthesize each sentence → float32 @ 24 kHz.
3. Concatenate with ~120 ms silence.
4. Record `(sentence, start_sample, end_sample)`.
5. Write **one** WAV to the cache dir from
   `StoragePaths.get_application_cache_directory()`.
6. Play it as one file and drive highlighting from `on_position_change`
   (1 s resolution is plenty for sentence-level highlighting).

Prefetch page *n+1* while page *n* plays. Evict by total cache size.
24 kHz mono WAV ≈ 2.9 MB/min, so cap the cache (e.g. 300 MB).

### Background audio (0b, the hard part)

`just_audio_background` requires more than a pubspec dependency:

1. **Manifest** — service + `MediaButtonReceiver`. Flet exposes permissions,
   application attributes, meta-data and providers, but not arbitrary
   `<service>` elements. Declare them in the extension's own Flutter plugin
   manifest (merged at build); fall back to the custom template if needed.
2. **Activity base class** — `MainActivity` must extend
   `AudioServiceFragmentActivity`. Flet supports `flet build --template <dir>`
   / `--template-ref`, so fork `flet-dev/flet-build-template` and change that
   one Kotlin line.
3. **Init** — `JustAudioBackground.init()` must run before the first player is
   created; call it lazily from the extension's Dart side, not from Python.

This mirrors what Flet PR #4558 did for `flet-video`, which was closed with
"implement it as a separate package" — proven approach, just not in core.

Python API stays clean:

```python
audio = BackgroundAudio(
    src=page_wav_path,          # asset or app-data path
    title="Chapter 3",
    artist="Soundbook",
    on_position=...,
    on_state=...,
    on_complete=...,
)
await audio.play()
```

## Risks

- **Android speed** — unmeasured. Gate on 0a.
- **Background audio** — hardest requirement; gate on 0b. Fallbacks if the
  activity replacement proves fragile: `audio_service` directly with a custom
  `BaseAudioHandler`, or `wakelock_plus` + `audioplayers` as a stopgap (no
  media notification).
- **APK size** — ~160–175 MB per ABI with all bundled assets (89 MB ONNX +
  27 MB voices + 13 MB dictionaries). Fine for
  sideloading; exceeds the 100 MB APK limit on Play. Use an AAB, or switch to
  download-on-first-run later.
- **First launch** — Flet copies app assets out of the APK, so expect a
  one-time delay for ~100 MB.
- **`extract_packages`** — if PyMuPDF or the ORT session can't read bundled
  data, add `[tool.flet.android] extract_packages = [...]`.
- **Licensing** — Kokoro weights are Apache-2.0; verify voice-pack terms before
  shipping. The dictionary G2P route also avoids espeak-ng's GPLv3.

## Commands

```bash
cd soundbook
uv sync
uv run flet run                 # desktop (requires desktop_flavor = "full")
uv run flet run --web           # server-mode web
uv run pytest

flet build apk --split-per-abi -v
flet build linux -v
flet build windows -v
flet build web -v
```