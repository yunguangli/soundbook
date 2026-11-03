"""Concatenate synthesized sentences into page audio and cache it."""

from __future__ import annotations

import io
import json
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from soundbook.core.tts.vocab import SAMPLE_RATE

DEFAULT_SILENCE = 0.12


@dataclass(frozen=True)
class SentenceTiming:
    text: str
    start: float
    end: float


@dataclass
class PageAudio:
    wav: bytes
    duration: float
    timings: list[SentenceTiming]

    def sentence_at(self, seconds: float) -> int:
        """Sentence playing at `seconds`; silence gaps map to the previous one."""
        index = 0
        for position, timing in enumerate(self.timings):
            if timing.start <= seconds:
                index = position
            else:
                break
        return index


def wav_bytes(samples: np.ndarray) -> bytes:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767).astype(np.int16)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


def wav_samples(data: bytes) -> np.ndarray:
    """Decode a mono 16-bit PCM WAV back to float32 samples."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        frames = wav.readframes(wav.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0


def combine_prefix(
    old: PageAudio,
    old_until: int,
    new: PageAudio,
    new_from: int = 0,
) -> PageAudio:
    """Keep the old audio up to a sentence, then continue with new audio.

    Used when the voice changes mid-page: the already-heard sentences keep the
    old voice, while the current and later sentences use the new voice. The
    returned timing list still covers every sentence in the page.
    """
    old_until = max(0, min(old_until, len(old.timings)))
    new_from = (
        max(0, min(new_from, len(new.timings) - 1)) if new.timings else 0
    )

    old_samples = wav_samples(old.wav)
    if old_until < len(old.timings):
        split = int(old.timings[old_until].start * SAMPLE_RATE)
    else:
        split = len(old_samples)
    prefix = old_samples[: max(0, split)]

    new_samples = wav_samples(new.wav)
    if new.timings and new_from < len(new.timings):
        new_split = new.timings[new_from].start
        start = int(new_split * SAMPLE_RATE)
    else:
        new_split = 0.0
        start = len(new_samples)
    tail = new_samples[max(0, start) :]

    combined = np.concatenate([prefix, tail]) if len(prefix) else tail
    if len(combined) == 0:
        combined = np.zeros(0, dtype=np.float32)

    # Put the first kept new sentence exactly at the end of the old prefix;
    # any silence before it in the new rendering is trimmed away.
    shift = len(prefix) / SAMPLE_RATE - new_split
    timings = list(old.timings[:old_until])
    timings.extend(
        SentenceTiming(
            text=timing.text,
            start=timing.start + shift,
            end=timing.end + shift,
        )
        for timing in new.timings[new_from:]
    )

    return PageAudio(
        wav=wav_bytes(combined),
        duration=len(combined) / SAMPLE_RATE,
        timings=timings,
    )


def assemble(
    parts: list[np.ndarray],
    sentences: list[str],
    silence_s: float = DEFAULT_SILENCE,
) -> PageAudio:
    if len(parts) != len(sentences):
        raise ValueError("parts and sentences must have the same length")

    silence = np.zeros(int(SAMPLE_RATE * silence_s), dtype=np.float32)
    chunks: list[np.ndarray] = []
    timings: list[SentenceTiming] = []
    cursor = 0.0

    for samples, sentence in zip(parts, sentences):
        start = cursor
        chunks.append(samples)
        cursor += len(samples) / SAMPLE_RATE
        timings.append(SentenceTiming(text=sentence, start=start, end=cursor))
        chunks.append(silence)
        cursor += silence_s

    if chunks:
        chunks.pop()  # no trailing silence

    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return PageAudio(
        wav=wav_bytes(audio),
        duration=len(audio) / SAMPLE_RATE,
        timings=timings,
    )


def render_page(
    tts,
    sentences: list[str],
    voice: str,
    speed: float = 1.0,
) -> PageAudio:
    parts = [tts.synthesize(sentence, voice=voice, speed=speed) for sentence in sentences]
    return assemble(parts, sentences)


class PageCache:
    """On-disk cache of rendered pages, one WAV + JSON per page."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _stem(self, book_id: int, chapter: int, page: int, voice: str, speed: float) -> str:
        return f"{book_id}-{chapter:04d}-{page:04d}-{voice}-{speed:.2f}"

    def wav_path(
        self, book_id: int, chapter: int, page: int, voice: str, speed: float
    ) -> Path:
        return self.directory / f"{self._stem(book_id, chapter, page, voice, speed)}.wav"

    def load(
        self, book_id: int, chapter: int, page: int, voice: str, speed: float
    ) -> PageAudio | None:
        stem = self._stem(book_id, chapter, page, voice, speed)
        wav_path = self.directory / f"{stem}.wav"
        meta_path = self.directory / f"{stem}.json"
        if not wav_path.exists() or not meta_path.exists():
            return None

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        timings = [SentenceTiming(**item) for item in meta["timings"]]
        return PageAudio(
            wav=wav_path.read_bytes(),
            duration=meta["duration"],
            timings=timings,
        )

    def has(
        self, book_id: int, chapter: int, page: int, voice: str, speed: float
    ) -> bool:
        """Whether a rendered page exists on disk, without reading it."""
        stem = self._stem(book_id, chapter, page, voice, speed)
        return (self.directory / f"{stem}.wav").exists() and (
            self.directory / f"{stem}.json"
        ).exists()

    def save(
        self,
        book_id: int,
        chapter: int,
        page: int,
        voice: str,
        speed: float,
        audio: PageAudio,
    ) -> None:
        stem = self._stem(book_id, chapter, page, voice, speed)
        (self.directory / f"{stem}.wav").write_bytes(audio.wav)
        (self.directory / f"{stem}.json").write_text(
            json.dumps(
                {
                    "duration": audio.duration,
                    "timings": [
                        {"text": t.text, "start": t.start, "end": t.end}
                        for t in audio.timings
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def delete_book(self, book_id: int) -> int:
        """Remove every cached page for one book."""
        freed = 0
        for path in self.directory.glob(f"{book_id}-*"):
            if path.is_file():
                freed += path.stat().st_size
                path.unlink()
        return freed

    def size_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.directory.glob("*"))

    def clear(self) -> int:
        freed = 0
        for path in self.directory.glob("*"):
            if path.is_file():
                freed += path.stat().st_size
                path.unlink()
        return freed