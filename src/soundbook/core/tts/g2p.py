"""Dictionary-based grapheme-to-phoneme conversion.

Uses misaki's English lexicons (`us_*` / `gb_*` JSON) without importing
misaki itself, which would pull in torch and transformers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

WORD_RE = re.compile(r"[A-Za-z']+|\d[\d,.]*|[.,!?;:]")

# Letter names for spelling out words that are absent from the lexicon.
SPELL = {
    "a": "ˈA", "b": "bˈi", "c": "sˈi", "d": "dˈi", "e": "ˈi", "f": "ˈɛf",
    "g": "ʤˈi", "h": "ˈAʧ", "i": "ˈI", "j": "ʤˈA", "k": "kˈA", "l": "ˈɛl",
    "m": "ˈɛm", "n": "ˈɛn", "o": "ˈO", "p": "pˈi", "q": "kjˈu", "r": "ˈɑɹ",
    "s": "ˈɛs", "t": "tˈi", "u": "jˈu", "v": "vˈi", "w": "dˈʌbəljˌu",
    "x": "ˈɛks", "y": "wˈI", "z": "zˈi",
}

ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
]
TENS = [
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
]
SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]


def _under_thousand(number: int) -> str:
    words: list[str] = []
    hundreds, rest = divmod(number, 100)
    if hundreds:
        words.append(f"{ONES[hundreds]} hundred")
    if rest >= 20:
        tens, ones = divmod(rest, 10)
        words.append(TENS[tens])
        if ones:
            words.append(ONES[ones])
    elif rest:
        words.append(ONES[rest])
    return " ".join(words)


def number_to_words(value: str) -> str:
    """Cardinal reading of a digit string, e.g. ``1912`` -> ``one thousand ...``."""
    value = value.replace(",", "").strip(".")
    if not value:
        return ""

    if "." in value:
        whole, fraction = value.split(".", 1)
        spoken = number_to_words(whole) if whole else "zero"
        digits = " ".join(ONES[int(d)] for d in fraction if d.isdigit())
        return f"{spoken} point {digits}"

    if len(value) > 1 and value.startswith("0"):
        return " ".join(ONES[int(d)] for d in value)

    number = int(value)
    if number == 0:
        return "zero"

    words: list[str] = []
    for scale, name in SCALES:
        count, number = divmod(number, scale)
        if count:
            words.append(f"{_under_thousand(count)} {name}")
    if number:
        words.append(_under_thousand(number))
    return " ".join(words)


@dataclass(frozen=True)
class Lexicon:
    words: dict[str, object]

    def lookup(self, word: str) -> str | None:
        """Return IPA for `word`.

        `us_gold.json` has 790 POS-keyed entries, e.g.
        `{"DEFAULT": "ˈɑˌɑ", "NOUN": null}`. Without a POS tagger, use DEFAULT.
        """
        value = self.words.get(word)
        if isinstance(value, dict):
            value = value.get("DEFAULT")
        return value if isinstance(value, str) and value else None


def load_lexicon(g2p_dir: Path | str, language: str = "en-us") -> Lexicon:
    """Load silver first, then gold overrides."""
    prefix = "gb" if language.lower().startswith("en-gb") else "us"
    words: dict[str, object] = {}
    for name in (f"{prefix}_silver", f"{prefix}_gold"):
        path = Path(g2p_dir) / f"{name}.json"
        with path.open(encoding="utf-8") as fh:
            words.update(json.load(fh))
    return Lexicon(words)


def _candidates(word: str) -> Iterator[str]:
    """Cheap morphological fallbacks for words missing from the lexicon.

    The dictionary has ~93k words but is not exhaustive (`families` is absent,
    `family` is present).
    """
    yield word
    if word.endswith("ies") and len(word) > 3:
        yield word[:-3] + "y"
    if word.endswith("es") and len(word) > 2:
        yield word[:-2]
    if word.endswith("s") and len(word) > 1:
        yield word[:-1]
    if word.endswith("ed") and len(word) > 2:
        yield word[:-2]
    if word.endswith("ing") and len(word) > 3:
        yield word[:-3]


def _spell(word: str) -> str:
    return " ".join(SPELL[char] for char in word if char in SPELL)


def phonemize(text: str, lexicon: Lexicon) -> str:
    """English text -> IPA string.

    Resolution order per token: lexicon -> morphological variant -> spelled
    letter names. Numbers are expanded to cardinal words first.
    """
    parts: list[str] = []
    for token in WORD_RE.findall(text):
        if token[0].isdigit():
            spoken = number_to_words(token)
            if spoken:
                parts.append(phonemize(spoken, lexicon))
            continue

        if token[0].isalpha() or token.startswith("'"):
            word = token.lower().strip("'")
            for candidate in _candidates(word):
                phonemes = lexicon.lookup(candidate)
                if phonemes:
                    parts.append(phonemes)
                    break
            else:
                spelled = _spell(word)
                if spelled:
                    parts.append(spelled)
        else:
            parts.append(token)
    return " ".join(parts)