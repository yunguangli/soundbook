# TTS model comparison for Soundbook

**Date:** 2026-11-08  
**Scope:** Offline, local TTS for Soundbook. Android is the priority; the same code should also work on Linux, Windows, and old PCs.  
**Status:** Assessment only. No production code was changed.

---

## Executive summary

**Paradee-8M v1.2 is the best fit for Soundbook's stated goal.**

It is the only candidate that combines all of the following:

- Apache-2.0 licence
- one 8.46 MB int8 ONNX file
- 24 kHz output, matching Soundbook's existing pipeline
- four voices, including the current default `af_heart`
- the exact Kokoro phoneme vocabulary already used by the app
- no new native dependency; it runs on the `onnxruntime` already bundled for Android
- roughly 10–12× realtime on a low-power 2015-class CPU, and about 24× aggregate with four parallel sentences
- ~253 MB RSS with one G2P lexicon, versus ~447 MB for Kokoro

Its costs are: only four voices, quality close to but below Kokoro (vendor UTMOS 4.40 versus 4.52 for `af_heart`, with the largest gap on `bf_isabella`), and slightly non-deterministic output because the ONNX graph contains a random-noise operator.

**Recommended product shape:**

1. **Paradee-8M v1.2** — default/fast engine, especially on Android and old PCs.
2. **Kokoro-82M** — optional high-quality / 54-voice engine, best kept as a separate download on capable devices.
3. **Leokani-tts-7M** — optional second small engine if its two voices are enough and the non-commercial licence is acceptable; its G2P is the best at names.
4. **PiVo-1.4M** — fallback only: Apache-2.0 and tiny, but 16 kHz and strict about unknown words.
5. **Phonon-31m** — desktop-only quality option; not practical on Android without native work.
6. **Underdog Woof** — not suitable for Soundbook; non-commercial and not concurrency-safe.

---

## Evaluation criteria

The comparison is weighted for Soundbook's actual constraints:

| Criterion | Why it matters |
|---|---|
| Android Python compatibility | Soundbook packages Python + `onnxruntime`; native Rust/NDK or missing wheels are a major cost. |
| Old-PC realtime factor | The app should not require a modern CPU to read a page. |
| 24 kHz pipeline fit | Soundbook's `audio.py`, WAV writer and cache assume 24 kHz. |
| Model size / APK impact | The app already ships large assets; an extra 100 MB is a product decision. |
| Voice quality | Audiobook listening is the primary use. |
| Number of voices | Users expect at least a few options; Kokoro currently exposes 54. |
| Licence | Bundling must be legally clean for the intended distribution. |
| Thread safety | The app synthesizes up to four sentences concurrently. |
| Speed control | The UI exposes a 0.5–2.0 speed slider. |
| OOV handling | Real books contain names, places and derived words. |
| Integration effort | The existing backend and GUI are built around Kokoro. |

---

## Current app baseline: Kokoro-82M

Soundbook currently uses `src/soundbook/core/tts/onnx_backend.py` with:

- `assets/kokoro/kokoro-v1.0.int8.onnx` — 92.4 MB
- `assets/kokoro/voices-v1.0.bin` — 28.2 MB
- `assets/g2p/{us,gb}_{gold,silver}.json` — 12.5 MB
- 24 kHz output
- 54 voices
- Apache-2.0
- `onnxruntime` on Android, Linux and Windows
- a voice dropdown only; there is no engine selector yet

On the test machine, Kokoro int8 runs at **0.5–0.9× realtime** and peaks at **~447 MB RSS**. It works, but it is heavy for low-end Android and old PCs.

---

## Candidates at a glance

| Model | Params | Runtime assets | Voices | Output | Licence | Android Python | Role |
|---|---:|---:|---:|---:|---|---|---|
| **Kokoro-82M** (current) | 81.8M | ~133 MB | 54 | 24 kHz | Apache-2.0 | ✅ | High-quality / many-voices engine |
| **Paradee-8M v1.2** | 7.88M | **8.46 MB** + existing G2P | 4 | 24 kHz | **Apache-2.0** | ✅ | **Recommended default** |
| **Leokani-tts-7M** | 7.0M | 27 MB (Dala) / 41 MB (both) | 2 | 24 kHz | CC BY-NC 4.0 | ✅ | Alternative if NC is acceptable |
| **PiVo-1.4M** | 1.4M | 9.1 MB | 2 | 16 kHz | Apache-2.0 + BSD/CMU | ✅ | Tiny fallback |
| **Phonon-31m** | 31M | ~85 MB incl. runtime | ~100 catalog | 24 kHz | Apache-2.0 | ❌ no wheel | Desktop-only quality option |
| **Underdog Woof** | — | 40.6 MB | 2 | 24 kHz | Non-commercial | ✅ (NumPy) | Rejected |

Asset sizes are approximate and use decimal MB. Paradee reuses Soundbook's existing G2P directory, so its marginal asset cost is only the 8.46 MB model.

---

## Measurements

### Test machine

- Intel Core m3-6Y30, 2 cores / 4 threads, 0.9–2.2 GHz
- Linux
- Python 3.12
- `onnxruntime` 1.31.1
- NumPy 2.5.4

This is a deliberately weak machine, representative of an old laptop. Speeds are **seconds of audio per second of compute**; higher is faster. RSS is the peak resident set size of the Python process and includes the interpreter, model, ONNX Runtime and G2P lexicons.

### Performance and memory

| Model | Configuration | Speed | Peak RSS | Notes |
|---|---|---:|---:|---|
| Kokoro-82M | int8, 2 threads | 0.88× | 447 MB | 0.54× at 1 thread; 0.57× at ORT default |
| **Paradee-8M v1.2** | int8, 1 thread | **9.9–12.6×** | **253 MB** | one `en-us` lexicon; 4-way aggregate ~24× |
| Leokani-7M | int8, 1 thread | 8.7× | 313 MB | 14.3× at 2 threads; 19.5× aggregate with 4-way |
| PiVo-1.4M | ONNX, 2 threads | 2.9× | 146 MB | 16 kHz output |
| Phonon-31m | q8, `ptts` | 1.7× | not measured | not thread-safe |
| Underdog Woof | int8 NumPy, 1 thread | 1.78× | 274 MB | seedable; concurrent calls corrupt output |

### Quality, as reported by the model authors

These numbers are not directly comparable across different papers and test sets. They are useful as a rough ordering only.

| Model / voice | UTMOS | WER |
|---|---:|---:|
| Kokoro `af_heart` | 4.52 | 5.7% |
| **Paradee `af_heart`** | **4.40** | **5.8%** |
| Kokoro `bf_isabella` | 4.41 | 5.6% |
| **Paradee `bf_isabella`** | **4.06** | **5.7%** |
| Kokoro `am_puck` | 4.47 | 6.3% |
| **Paradee `am_puck`** | **4.32** | **5.4%** |
| Kokoro `bm_fable` | 4.32 | 5.7% |
| **Paradee `bm_fable`** | **4.18** | **5.7%** |
| Leokani Dala | 3.61 | 0.07%* |
| Leokani Pono | 3.19 | 0.48%* |

\* Leokani's WER uses a different Whisper model and evaluation set, so it is not comparable to the Kokoro/Paradee rows.

Paradee is close to its Kokoro teacher, with the largest gap on `bf_isabella`. Leokani is intelligible but less natural by its vendor's UTMOS. PiVo and Phonon have their own vendor claims; they were not re-scored here.

### Local A/B samples

Generated during this assessment, outside the repository:

- `/tmp/opencode/paradee_samples/paradee-{af_heart,bf_isabella,am_puck,bm_fable}.wav`
- `/tmp/opencode/paradee_samples/kokoro-{af_heart,bf_isabella,am_puck,bm_fable}.wav`
- `/tmp/opencode/leokani_samples/leokani-dala-int8.wav`
- `/tmp/opencode/leokani_samples/leokani-pono-fp16.wav`
- `/tmp/opencode/leokani_samples/kokoro-{oldg2p,leokanig2p}-*.wav`

Listen to the Paradee/Kokoro pairs before deciding; the published scores are a guide, not a substitute.

---

## Detailed reviews

### 1. Paradee-8M v1.2 — recommended

**What it is**

- 7.88M parameters, distilled from Kokoro-82M
- one int8 ONNX file, 8.46 MB, containing all four voices
- four voices: `af_heart` (US female), `am_puck` (US male), `bf_isabella` (GB female), `bm_fable` (GB male)
- 24 kHz mono float32 output
- Apache-2.0
- ONNX inputs: `input_ids` (int64, Kokoro phoneme IDs with a 0 pad at each end), `speed` (float32), `voice` (int64 index); output: `waveform`
- opset 17, IR 8, standard ONNX operators
- graph contains `RandomNormalLike`, so output is slightly non-deterministic

**Why it fits Soundbook**

- **Exact vocabulary match.** Paradee's `config.json` vocab has the same 114 entries and the same IDs as `src/soundbook/core/tts/vocab.py`. `tokenize()` works unchanged.
- **Reuses the existing G2P.** The app already ships `us_*` and `gb_*` lexicons. Paradee's British voices need British phonemes; Soundbook's `en-gb` lexicon produces exactly the expected forms (`bˈɑːθ`, `ɡˌQ`).
- **24 kHz, no resampling.** `assemble()` and `wav_bytes()` work unchanged.
- **No new dependency.** `onnxruntime` is already a per-platform dependency.
- **Voice switching is free.** All four voices share one graph; changing `voice` does not reload the model.
- **Speed is native.** `speed=0.5…2.0` maps directly; at 2.0 the output is ~1.83× faster, with mild saturation similar to Leokani.
- **Tiny.** Adding it beside Kokoro costs 8.46 MB. Replacing Kokoro removes the 92.4 MB model and the 28.2 MB voices archive.

**Measured**

- model load: 1.7 s
- first synthesis: 0.56 s
- 6.08 s of audio in 0.49–0.61 s at 1 thread: **9.9–12.6× realtime**
- four concurrent sentences: 10.12 s of audio in 0.42 s: **~24× aggregate**
- RSS: **~253 MB** with one G2P lexicon

**Caveats**

- **Only four voices.** Users who chose one of Kokoro's other 50 voices would need the Kokoro engine or a different voice.
- **Slightly non-deterministic.** `RandomNormalLike` has no seed input in the released graph. Two runs on the same text have the same length but differ in the noisy parts (waveform correlation ~0.94, SNR ~9 dB). This is fine for playback and for the page cache, but tests must not compare exact waveforms.
- **OOV pronunciation is only as good as the current G2P.** Names such as `Okafor`, `Nguyen` and `Siobhan` are spelled out by Soundbook's dictionary fallback. Leokani G2P fixes those, but adds 19 MB and ~225 MB RSS; a small user lexicon is the cheaper compromise.
- **Input limit.** The model allows 510 phonemes plus two pads; Soundbook's `tokenize()` currently caps at 507. A single unpunctuated block over the limit raises `ValueError`, as it already can with Kokoro. The adapter should split long sentences by phoneme count, not only by character count.
- **Android speed is not directly measured here.** The model is 7.88M parameters and ONNX-only, so it is expected to be comfortably realtime; the vendor reports ~62× on an Apple M4 Pro. Validate on the target phone.

**Integration effort: low**

- Add `assets/paradee/paradee_int8.onnx`, `config.json`, `LICENSE`.
- Reuse `assets/g2p` and `voices.py`; no `voices-v1.0.bin` needed.
- New `ParadeeTTS` adapter: load one session, map voice names to indices, call `phonemize()` + `tokenize()`, run the session, return float32.
- Make `_TTS_MODELS` engine-aware and filter the voice list.
- Exclude `assets/paradee` from the web build.

---

### 2. Leokani-tts-7M — strong alternative, licence-limited

**What it is**

- 7.0M parameters, 24 kHz, English only
- two voices: Dala (female) and Pono (male)
- ONNX int8 Dala: 7.84 MB; Pono only fp16/fp32 (14.4 MB / 28 MB)
- Leokani G2P: 19.2 MB of code, lexicon and three ONNX models
- total: ~27 MB for Dala, ~41 MB for both
- model licence: **CC BY-NC 4.0**; G2P code MIT, G2P weights/lexicon CC BY 4.0
- generated audio carries an inaudible watermark

**Measured**

- 8.7× realtime at 1 thread; 14.3× at 2 threads; 19.5× aggregate with 4-way sentence parallelism
- ~313 MB RSS with G2P
- deterministic and thread-safe
- all 295 Chesterton sentences passed the front end with 0 errors and 0 empty results; three full pages synthesized cleanly
- names, numbers and dates handled by the neural G2P; a user lexicon is available

**Why it is attractive**

- No OOV failures, unlike PiVo.
- Excellent G2P: `Okafor` → `ˈɑkæfəɹ`, `Nguyen` → `wˈɪn`, `Siobhan` → `ʃɪvˈɔn`.
- 24 kHz, no resampling.
- Android-friendly: pure Python + ONNX, no native build.

**Caveats**

- **Non-commercial licence.** This is the main blocker for a commercial app. A commercial licence must be negotiated with Milliwatt Labs.
- **Two voices only.**
- **Pono has no int8 file**, so both voices cost 14.4 MB more.
- **Memory.** The 19.2 MB G2P expands to ~225 MB RSS. Dala + G2P peaks at ~313 MB, still below Kokoro but substantial.
- **`synthesize("")` raises `SystemExit`**, not `ValueError`; the adapter must guard empty/punctuation-only text.
- **Lazy G2P race.** The first concurrent calls can construct the G2P twice; load it eagerly or guard it.
- **Watermark.** Inaudible, but should be disclosed in the About/licence notes.

**Integration effort: medium**

- New model assets and G2P assets.
- New adapter and voice list.
- Engine-aware cache/model keys.
- Eager G2P warm-up and empty-text guard.
- NC licence notice in Settings/About.

**Role:** the best alternative if the project is non-commercial and two voices are enough. Its G2P can also be used with other Kokoro-vocabulary models under MIT/CC BY 4.0.

---

### 3. PiVo-1.4M — tiny Apache fallback

**What it is**

- 1.4M parameters, ONNX, ~9.1 MB
- two voices: Pi and Vo
- 16 kHz float32 output
- Apache-2.0 (plus BSD FARGAN and CMU dictionary notices)
- native `speed` 0.5–2.0
- strict CMU-dictionary frontend: unknown words raise `ValueError` unless overrides are supplied

**Measured**

- 2.9× realtime at 2 threads
- ~146 MB RSS
- deterministic and thread-safe in tests
- 21% of the 295 Chesterton sentences contained at least one OOV word; `lodger`, `tidiness`, `practising`, `ventriloquist`, `physiologists`, `geographies` were missing from its dictionary

**Why it might be considered**

- One of the smallest Apache-2.0 models by parameter count, with a very small asset footprint.
- Very low memory.
- Simple API and native speed.
- Good fit if 16 kHz is acceptable and the app supplies pronunciation overrides or a fallback.

**Caveats**

- **16 kHz → 24 kHz resampling is mandatory.** Feeding 16 kHz samples into `assemble()` as 24 kHz makes playback 1.5× too fast and breaks sentence timings.
- **Strict OOV.** Needs a spelling fallback, morphological rules, a user lexicon, or fallback to another engine.
- **Quality** is below Kokoro/Paradee/Leokani in the vendor's own description (metallic/robotic on some sentences).
- **Two voices.**

**Integration effort: medium**

- Adapter plus resampling.
- OOV strategy.
- Model selector and cache keys.

**Role:** fallback if only Apache-2.0 and minimum size matter; not the best listening experience.

---

### 4. Phonon-31m — desktop quality, Android blocker

**What it is**

- 31M parameters, 24 kHz
- `model.q8.gguf` 64.5 MB, tokenizer/voice ~0.6 MB, `ptts` native runtime ~19.5 MB
- ~100 catalog voices, 37 English; the repo ships one default voice
- Apache-2.0 for the 31m model, tokenizer and voices; runtime MIT/Apache-2.0
- native `speed` via `duration_delta`, which is non-linear and saturates
- **not thread-safe**; use one call at a time or a lock

**Measured**

- 1.7× realtime on the test machine
- 24 kHz, no resampling
- Python wheels exist for Linux, macOS, Windows, but **no Android wheel**

**Why it is attractive on desktop**

- Best published quality and voice variety of the candidates.
- Apache-2.0.
- 24 kHz and a small adapter.

**Why it does not fit Android**

- `ptts` is a Rust/GGUF runtime. Android requires building `libptts_ffi.so` with Rust + `cargo-ndk`, packaging it in the APK, and calling it through a Flet extension. That is a separate project, comparable to the background-audio work already planned.
- No thread safety; the app's four-way sentence parallelism would need a lock, reducing throughput.

**Integration effort: low on desktop, high on Android**

**Role:** optional desktop-only high-quality engine. Do not make it the cross-platform default.

---

### 5. Kokoro-82M — existing baseline

**Strengths**

- 54 voices, including all four Paradee voices.
- Highest quality/UTMOS of the tested set.
- Apache-2.0.
- Already integrated, tested and shipped.
- 24 kHz, same G2P and audio pipeline.

**Weaknesses**

- 133 MB of assets.
- ~447 MB RSS.
- 0.5–0.9× realtime on the test machine; marginal on old PCs and low-end Android.
- No engine selector in the current GUI; all voices are Kokoro.

**Role:** keep as an optional high-quality / many-voices engine. On constrained devices, make it a separate download rather than bundling it with Paradee. The existing `af_heart` default migrates directly to Paradee.

---

### 6. Underdog Woof — not suitable for Soundbook

**What it is**

- 40.6 MB int8 `.npz` weights
- two voices: Juno and Mira
- 24 kHz
- pure NumPy runtime, no ONNX, no native library
- seedable output
- **Underdog Non-Commercial License 1.0**; commercial use requires a licence
- no speed control
- concurrent calls corrupt output because the decoder shares state

**Measured**

- 1.78× realtime on the test machine
- ~274 MB RSS
- 5.96 s of audio in 3.34 s
- seeded runs are reproducible; unseeded runs differ

**Why it is not suitable**

- Non-commercial licence.
- Slower than Paradee/Leokani and heavier than necessary for its quality.
- Not thread-safe; needs a global lock, which conflicts with the app's parallel sentence rendering.
- No native speed control.
- The NumPy runtime is a maintenance burden compared with the ONNX stack the app already uses.

**Role:** rejected for Soundbook. It can be used in a separate project if its voice/style is wanted.

---

## Platform fit

| Platform | Kokoro | Paradee | Leokani | PiVo | Phonon | Woof |
|---|---|---|---|---|---|---|
| Android (Python/ONNX) | ✅ but heavy | ✅ **best** | ✅ | ✅ | ❌ no wheel | ✅ but NumPy/rejected |
| Linux | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Windows | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Old PCs | ⚠️ <1× on test machine | ✅ **10×+** | ✅ 8×+ | ✅ 3× | ⚠️ 1.7× | ⚠️ 1.8× |
| Web / Flet Pyodide | ❌ assets excluded | ❌ ONNX unavailable | ❌ | ❌ | ❌ | ⚠️ pure Python but not integrated |
| macOS | ✅ if packaged | ✅ if packaged | ✅ if packaged | ✅ if packaged | ⚠️ CoreML only | ✅ |

The current web build is server-backed and excludes the Kokoro/G2P assets. None of these engines should be shipped into Pyodide.

---

## Licence fit

| Model | Main licence | Commercial use | Attribution / notices |
|---|---|---|---|
| Kokoro-82M | Apache-2.0 | ✅ | Include Apache-2.0 notice |
| **Paradee-8M v1.2** | **Apache-2.0** | ✅ | Include Apache-2.0 notice; model is distilled from Kokoro |
| Leokani-tts-7M | CC BY-NC 4.0 | ❌ without a commercial licence | Credit Milliwatt Labs; NC terms; watermark |
| Leokani G2P | MIT code; CC BY 4.0 weights/lexicon | ✅ | Credit Milliwatt Labs, GeoNames, CMU, misaki/Kokoro |
| PiVo-1.4M | Apache-2.0 + BSD/CMU | ✅ | Include `LICENSE`, `NOTICE`, `LICENSES/` |
| Phonon-31m | Apache-2.0; runtime MIT/Apache-2.0 | ✅ | Include notices |
| Underdog Woof | Underdog Non-Commercial 1.0 | ❌ without a licence | Credit Underdog in app/title |

**Bottom line:** Paradee is the only small model here with a clean Apache-2.0 licence, 24 kHz output, Android compatibility and good old-PC performance.

---

## Integration effort

| Model | Work required | Risk |
|---|---|---|
| **Paradee** | New 50-line adapter; reuse G2P/tokenizer/audio; engine-aware keys; filter 4 voices; handle non-determinism; add 8.46 MB asset | **Low** |
| Leokani | New adapter + 19.2 MB G2P; eager G2P; empty guard; NC notices; engine selector | Medium |
| PiVo | Adapter + 16→24 kHz resampler; OOV overrides/fallback; engine selector | Medium |
| Phonon | Desktop dependency and lock; Android needs Rust/NDK + Flet extension | High on Android |
| Kokoro | Already integrated; needs engine selector if kept alongside Paradee | Low |
| Woof | NumPy runtime, global lock, no speed; non-commercial | High / rejected |

---

## Recommendation

### Default architecture

Use an **engine registry** rather than treating every model as a voice:

- `ParadeeTTS` — default on Android and old PCs; voices `af_heart`, `am_puck`, `bf_isabella`, `bm_fable`.
- `OnnxTTS` (Kokoro) — optional high-quality / 54-voice engine on capable devices.
- `LeokaniTTS` — optional second small engine if the NC licence is acceptable and two voices are enough.
- `PiVoTTS` — optional tiny Apache fallback, with resampling and OOV handling.
- `PhononTTS` — desktop-only optional; do not bundle for Android.
- `WoofTTS` — do not include.

Key the TTS cache by `(engine, language, voice, speed)`, not just language. The current `_TTS_MODELS` keyed only by language would return Kokoro when Paradee's `af_heart` is requested, because both are `en-us`.

### Why Paradee should be the default

- It meets the "all platforms, including old PCs" goal. Kokoro does not; it is below realtime on the test machine.
- It meets the Android priority with no native work.
- It is 15× smaller than Kokoro's runtime assets.
- It is Apache-2.0, so there is no licensing negotiation.
- It reuses the existing G2P and tokenizer exactly, so the integration is small.
- Its quality is close to Kokoro on the vendor's own evaluation, and the four voices include the current default.

### When to choose something else

- **Need 54 voices or maximum quality on a capable desktop:** keep Kokoro selectable.
- **Need the best name pronunciation and can accept CC BY-NC 4.0:** Leokani.
- **Need the smallest parameter count / lowest memory under Apache-2.0:** PiVo, with 16 kHz and OOV caveats.
- **Desktop-only and quality is paramount:** Phonon.
- **Woof's voice/style is specifically wanted:** separate project, non-commercial.

### Hybrid option: Paradee + Leokani G2P

Paradee and Leokani G2P use the same Kokoro phoneme vocabulary, so they can be combined:

- `af_heart` / `am_puck`: Leokani G2P for much better name pronunciation (`Okafor`, `Nguyen`, `Siobhan`).
- `bf_isabella` / `bm_fable`: keep Soundbook's `en-gb` G2P, because Leokani G2P is American-English oriented and Paradee's British voices need British phonemes.

This combination is commercially clean (Paradee Apache-2.0; Leokani G2P MIT/CC BY 4.0), but costs an extra 19 MB and ~225 MB RSS. It is a good desktop option and a possible optional "enhanced pronunciation" pack on Android, not a default.

---

## Paradee implementation plan (if accepted)

### Assets

```text
src/assets/paradee/
  paradee_int8.onnx      # 8.46 MB, all four voices
  config.json
  LICENSE
```

Keep `src/assets/g2p/` as-is. Do not ship `voices-v1.0.bin` for Paradee.

### Adapter sketch

```python
class ParadeeTTS:
    VOICES = ("af_heart", "bf_isabella", "am_puck", "bm_fable")

    def __init__(self, model_path, g2p_dir, threads=1):
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self.g2p_dir = g2p_dir
        self._lexicons = {}

    def voice_names(self):
        return list(self.VOICES)

    def _lexicon(self, language):
        if language not in self._lexicons:
            self._lexicons[language] = load_lexicon(self.g2p_dir, language)
        return self._lexicons[language]

    def synthesize(self, text, voice="af_heart", speed=1.0):
        language = "en-gb" if voice.startswith("b") else "en-us"
        ids = tokenize(phonemize(text, self._lexicon(language)))
        voice_index = self.VOICES.index(voice)
        audio = self.session.run(None, {
            "input_ids": ids,
            "speed": np.asarray([speed], dtype=np.float32),
            "voice": np.asarray([voice_index], dtype=np.int64),
        })[0]
        return np.asarray(audio, dtype=np.float32).reshape(-1)
```

Notes:

- `tokenize()` already adds the `0` pad at each end.
- Do not depend on the upstream `paradee` Python package. It pulls `huggingface_hub` and `misaki` and downloads assets at runtime. Use the ONNX file directly and keep Soundbook's existing offline G2P.
- The upstream package byte-patches the ONNX graph to expose per-token durations for word timestamps. Soundbook already computes sentence timings from the rendered audio, so the unpatched model is sufficient.
- Add a guard for empty/punctuation-only text.
- Split a sentence if `tokenize()` would exceed 507 phonemes.
- Warm the session and one lexicon in `_warm_model()`.
- Because output is non-deterministic, cache the first render; tests should compare length/energy, not exact samples.

### App changes

1. `config.py` — add `paradee_dir()`, `paradee_model_path()`.
2. `voices.py` — add `list_paradee_voices()`.
3. `app.py`:
   - `_TTS_MODELS` key becomes `(engine, language)`.
   - `_ensure_model()` and `_warm_model()` compare engine + language.
   - `_load_tts()` routes `af_heart`, `am_puck`, `bf_isabella`, `bm_fable` to Paradee when the Paradee engine is selected, otherwise Kokoro.
   - `_voice_menu_items()` filters by engine.
   - Persist `tts_engine` as a setting; default `af_heart` remains valid for both.
4. `PageCache._stem` — include engine.
5. `pyproject.toml` — add `assets/paradee` to `[tool.flet.web.app].exclude`.
6. `.gitignore` — add `src/assets/paradee/`.
7. About/Licences — add Paradee Apache-2.0 and Kokoro attribution.

### Tests

- four voices return 24 kHz float32
- `af_heart` and `bf_isabella` use the correct G2P language
- `speed` changes duration in the expected direction
- four concurrent sentences do not error and have stable lengths
- page cache key differs by engine/voice/speed
- overlong sentence is split instead of raising
- non-determinism is tolerated (length/energy, not exact equality)

---

## Open questions

1. **Is four voices enough?** If not, keep Kokoro as an optional engine. The four Paradee voices cover the current default plus one US male and two British voices.
2. **Is the slight non-determinism acceptable?** It is inaudible in normal use and hidden by the page cache, but it affects tests and re-renders.
3. **Does Paradee meet the listening bar on real books?** The vendor scores are close to Kokoro; listen to the A/B samples before committing.
4. **What is the real Android speed?** Test on the oldest supported phone. The 8M ONNX model should be realtime; the vendor's Android number is not published.
5. **Should Kokoro remain bundled?** Bundling both adds only 8.46 MB (Paradee) but keeps 133 MB of Kokoro assets. A separate Kokoro download is the better size/quality trade-off.
6. **Use Leokani G2P for names?** It fixes `Okafor`/`Nguyen`/`Siobhan`, but adds 19 MB and ~225 MB RSS. A small user lexicon or an OOV-only path is cheaper.
7. **Web build:** remains server-backed; do not ship any of these ONNX assets to Pyodide.

---

## References

- Kokoro-82M: <https://huggingface.co/hexgrad/Kokoro-82M>
- Paradee-8M v1.2: <https://huggingface.co/sahilmahendrakar/Paradee-8M-v1.2>
- Paradee code: <https://github.com/sahilmahendrakar/paradee>
- Leokani-tts-7M: <https://huggingface.co/milliwattlabs/leokani-tts-7M>
- PiVo-1.4M: <https://huggingface.co/empero-ai/PiVo-1.4M>
- Phonon-31m-en: <https://huggingface.co/gradium/phonon-31m-en>
- Underdog Woof: <https://huggingface.co/ConwayResearch/Underdog-Woof>

## Related Soundbook files

- `src/soundbook/core/tts/onnx_backend.py` — current Kokoro backend
- `src/soundbook/core/tts/g2p.py` — dictionary G2P
- `src/soundbook/core/tts/vocab.py` — Kokoro vocab and `tokenize()`
- `src/soundbook/core/tts/voices.py` — Kokoro voice list
- `src/soundbook/core/audio.py` — 24 kHz assembly, WAV writer, `PageCache`
- `src/soundbook/config.py` — asset paths
- `src/soundbook/app.py` — `_load_tts`, `_warm_model`, `_ensure_model`, voice menu, cache keys
- `pyproject.toml` — per-platform `onnxruntime` and web exclusions
- `.gitignore` — bundled TTS assets