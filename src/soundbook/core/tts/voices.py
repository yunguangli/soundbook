"""Read voice names from the voices archive without loading a model."""

from __future__ import annotations

from pathlib import Path

import numpy as np


def list_voices(voices_path: Path | str) -> list[str]:
    with np.load(voices_path) as data:
        return list(data.files)