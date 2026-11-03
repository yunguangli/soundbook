"""Kokoro TTS backends."""

from soundbook.core.tts.g2p import Lexicon, load_lexicon, phonemize

__all__ = ["Lexicon", "load_lexicon", "phonemize"]