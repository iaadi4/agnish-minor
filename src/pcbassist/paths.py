"""Locations of bundled data (works from a source checkout and from an installed wheel)."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("PCBASSIST_DATA_DIR")
    if env:
        return Path(env)
    # src/pcbassist/paths.py -> repo root / data
    return Path(__file__).resolve().parents[2] / "data"


def fixtures_dir() -> Path:
    env = os.environ.get("PCBASSIST_FIXTURES_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "fixtures"
