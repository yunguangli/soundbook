import numpy as np

from soundbook.core.audio import PageCache, SentenceTiming, assemble, wav_bytes
from soundbook.core.tts.vocab import SAMPLE_RATE


def test_assemble_timings():
    one_second = np.zeros(SAMPLE_RATE, dtype=np.float32)
    page = assemble([one_second, one_second], ["one", "two"], silence_s=0.1)

    assert len(page.timings) == 2
    assert page.timings[0].start == 0.0
    assert page.timings[0].end == 1.0
    assert page.timings[1].start == 1.1
    assert page.timings[1].end == 2.1
    assert page.duration == 2.1


def test_sentence_at():
    one_second = np.zeros(SAMPLE_RATE, dtype=np.float32)
    page = assemble([one_second, one_second], ["one", "two"], silence_s=0.1)

    assert page.sentence_at(0.5) == 0
    assert page.sentence_at(1.05) == 0  # still in the silence gap
    assert page.sentence_at(1.5) == 1


def test_wav_bytes_header():
    data = wav_bytes(np.zeros(100, dtype=np.float32))
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"


def test_cache_roundtrip(tmp_path):
    cache = PageCache(tmp_path)
    page = assemble(
        [np.zeros(SAMPLE_RATE, dtype=np.float32)],
        ["hello"],
        silence_s=0.0,
    )

    assert cache.load(1, 0, 0, "af_heart", 1.0) is None
    cache.save(1, 0, 0, "af_heart", 1.0, page)

    loaded = cache.load(1, 0, 0, "af_heart", 1.0)
    assert loaded is not None
    assert loaded.duration == page.duration
    assert loaded.timings == [SentenceTiming("hello", 0.0, 1.0)]
    assert loaded.wav == page.wav


def test_cache_clear(tmp_path):
    cache = PageCache(tmp_path)
    page = assemble([np.zeros(10, dtype=np.float32)], ["x"], silence_s=0.0)
    cache.save(1, 0, 0, "af_heart", 1.0, page)

    assert cache.size_bytes() > 0
    assert cache.clear() > 0
    assert cache.size_bytes() == 0