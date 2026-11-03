import io
import wave

import numpy as np
import pytest

from soundbook.core.tts.onnx_backend import OnnxTTS
from soundbook.core.tts.vocab import MAX_PHONEMES, SAMPLE_RATE, tokenize


def test_tokenize_wraps_bos_eos():
    ids = tokenize("hˈɛlO")
    assert ids.shape[0] == 1
    assert ids[0, 0] == 0
    assert ids[0, -1] == 0
    assert ids.dtype == np.int64


def test_tokenize_drops_unknown_characters():
    assert tokenize("§").shape == (1, 2)


def test_tokenize_rejects_overlong_input():
    with pytest.raises(ValueError):
        tokenize("a" * (MAX_PHONEMES + 1))


def test_wav_bytes_is_valid_pcm():
    samples = np.zeros(SAMPLE_RATE // 10, dtype=np.float32)
    data = OnnxTTS.wav_bytes(samples)

    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnframes() == len(samples)