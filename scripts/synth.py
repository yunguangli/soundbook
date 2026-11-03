#!/usr/bin/env python3
"""Synthesize text to a WAV file.

    uv run --with onnxruntime python scripts/synth.py "Hello there." -o out.wav
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from soundbook import config  # noqa: E402
from soundbook.core.tts.onnx_backend import OnnxTTS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", default="Hello from Soundbook.")
    parser.add_argument("-o", "--output", default="/tmp/opencode/soundbook.wav")
    parser.add_argument("-v", "--voice", default="af_heart")
    parser.add_argument("-s", "--speed", type=float, default=1.0)
    parser.add_argument(
        "-l", "--language", default="en-us", choices=["en-us", "en-gb"]
    )
    args = parser.parse_args()

    t0 = time.perf_counter()
    tts = OnnxTTS(
        model_path=config.model_path(),
        voices_path=config.voices_path(),
        g2p_dir=config.g2p_dir(),
        language=args.language,
    )
    load_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    samples = tts.synthesize(args.text, voice=args.voice, speed=args.speed)
    synth_s = time.perf_counter() - t0

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(OnnxTTS.wav_bytes(samples))

    duration = len(samples) / 24_000
    print(f"ipa   : {tts.phonemize(args.text)}")
    print(f"load  : {load_s:.2f} s")
    print(f"synth : {synth_s:.2f} s")
    print(f"audio : {duration:.2f} s ({synth_s / duration:.2f}x RTF)")
    print(f"out   : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())