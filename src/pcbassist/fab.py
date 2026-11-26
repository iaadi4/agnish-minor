"""Fab/assembly profile loading (``data/fab_profiles/<name>.json``)."""

from __future__ import annotations

import json
from functools import cache

from .paths import data_dir
from .schemas import FabProfile


@cache
def load_fab_profile(name: str) -> FabProfile:
    path = data_dir() / "fab_profiles" / f"{name}.json"
    if not path.exists():
        raise ValueError(f"unknown fab profile '{name}' (no {path})")
    return FabProfile.model_validate(json.loads(path.read_text()))


def list_fab_profiles() -> list[str]:
    return sorted(p.stem for p in (data_dir() / "fab_profiles").glob("*.json"))
