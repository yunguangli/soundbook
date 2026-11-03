"""Kokoro inference through onnxruntime.

No PyTorch, transformers, phonemizer or espeak. Works with the int8 v1.0
export shipped in `assets/kokoro`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnxruntime as ort

from soundbook.core.audio import wav_bytes as _wav_bytes
from soundbook.core.tts.g2p import Lexicon, load_lexicon, phonemize
from soundbook.core.tts.vocab import tokenize


class OnnxTTS:
    def __init__(
        self,
        model_path: Path | str,
        voices_path: Path | str,
        g2p_dir: Path | str,
        language: str = "en-us",
        threads: int = 0,
    ) -> None:
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(
            str(model_path),
            options,
            providers=["CPUExecutionProvider"],
        )

        input_names = {i.name for i in self.session.get_inputs()}
        # v1.0 names the token input "tokens"; newer exports use "input_ids".
        self._id_name = "tokens" if "tokens" in input_names else "input_ids"

        self.voices = np.load(voices_path)
        self.language = language
        self.lexicon: Lexicon = load_lexicon(g2p_dir, language)

    def voice_names(self) -> list[str]:
        return list(self.voices.files)

    def phonemize(self, text: str) -> str:
        return phonemize(text, self.lexicon)

    def synthesize(
        self,
        text: str,
        voice: str = "af_heart",
        speed: float = 1.0,
    ) -> np.ndarray:
        """Text -> float32 mono samples at 24 kHz."""
        if voice not in self.voices:
            raise ValueError(f"unknown voice {voice!r}")

        ids = tokenize(self.phonemize(text))
        style = np.asarray(
            self.voices[voice][ids.shape[1]], dtype=np.float32
        ).reshape(1, -1)

        audio = self.session.run(
            None,
            {
                self._id_name: ids,
                "style": style,
                "speed": np.asarray([speed], dtype=np.float32),
            },
        )[0]
        return np.asarray(audio, dtype=np.float32).reshape(-1)

    @staticmethod
    def wav_bytes(samples: np.ndarray) -> bytes:
        """Encode float32 samples as 16-bit mono PCM WAV."""
        return _wav_bytes(samples)