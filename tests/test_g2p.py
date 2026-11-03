from pathlib import Path

import pytest

from soundbook.core.tts.g2p import load_lexicon, number_to_words, phonemize

G2P_DIR = Path(__file__).resolve().parents[1] / "src" / "assets" / "g2p"


@pytest.fixture(scope="module")
def lexicon():
    return load_lexicon(G2P_DIR)


def test_phonemize_basic_sentence(lexicon):
    result = phonemize("The quick brown fox.", lexicon)
    assert result
    assert "fˈɑks" in result
    assert result.endswith(".")


def test_morphological_fallback(lexicon):
    # "families" is absent from both dictionaries; "family" is present.
    assert lexicon.lookup("families") is None

    result = phonemize("families", lexicon)
    assert result
    assert result != "families"


def test_pos_keyed_entry_uses_default(lexicon):
    value = lexicon.words["AA"]
    assert isinstance(value, dict)
    assert lexicon.lookup("AA") == value["DEFAULT"]


def test_unknown_word_is_spelled(lexicon):
    result = phonemize("qzxvplk", lexicon)
    assert result
    assert result != "qzxvplk"


def test_numbers_are_expanded(lexicon):
    assert number_to_words("1912") == "one thousand nine hundred twelve"
    assert number_to_words("7") == "seven"
    assert number_to_words("3.14") == "three point one four"

    spoken = phonemize("1912", lexicon)
    assert spoken
    assert spoken == phonemize("one thousand nine hundred twelve", lexicon)


def test_punctuation_preserved(lexicon):
    result = phonemize("Yes, really?", lexicon)
    assert "," in result
    assert "?" in result