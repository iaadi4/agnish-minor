"""Resolve KiCad footprint names (``Library:Footprint``) against a known index.

Resolution order:
1. A local KiCad installation (``KICAD{10,9,8}_FOOTPRINT_DIR`` env vars or standard paths).
2. The bundled index ``data/kicad/footprint_index.json`` (built from the official
   kicad-footprints repository by ``scripts/build_seed.py --index``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..paths import data_dir

_STD_PATHS = [
    "/usr/share/kicad/footprints",
    "/usr/local/share/kicad/footprints",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    "C:/Program Files/KiCad/10.0/share/kicad/footprints",
    "C:/Program Files/KiCad/9.0/share/kicad/footprints",
    "C:/Program Files/KiCad/8.0/share/kicad/footprints",
]


@dataclass(frozen=True)
class Resolution:
    name: str
    resolved: bool
    source: str
    suggestion: str | None = None


class FootprintResolver:
    def __init__(self, index: dict[str, set[str]], source: str) -> None:
        self.index = index
        self.source = source

    @classmethod
    def from_kicad_dir(cls, root: Path) -> FootprintResolver:
        idx: dict[str, set[str]] = {}
        for lib in root.glob("*.pretty"):
            idx[lib.name[: -len(".pretty")]] = {p.stem for p in lib.glob("*.kicad_mod")}
        return cls(idx, f"local KiCad install ({root})")

    @classmethod
    def from_bundled_index(cls, path: Path | None = None) -> FootprintResolver:
        p = path or data_dir() / "kicad" / "footprint_index.json"
        raw = json.loads(p.read_text())
        libs: dict[str, list[str]] = raw["libraries"]
        return cls(
            {k: set(v) for k, v in libs.items()},
            f"bundled index (kicad-footprints {raw.get('ref', '?')})",
        )

    @classmethod
    def detect(cls) -> FootprintResolver:
        for var in ("KICAD10_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR", "KICAD8_FOOTPRINT_DIR"):
            v = os.environ.get(var)
            if v and Path(v).is_dir():
                return cls.from_kicad_dir(Path(v))
        for sp in _STD_PATHS:
            if Path(sp).is_dir() and any(Path(sp).glob("*.pretty")):
                return cls.from_kicad_dir(Path(sp))
        return cls.from_bundled_index()

    def resolve(self, name: str | None) -> Resolution:
        if not name or ":" not in name:
            return Resolution(
                name or "", False, self.source, "footprint must be 'Library:Footprint'"
            )
        lib, fp = name.split(":", 1)
        names = self.index.get(lib)
        if names is None:
            return Resolution(name, False, self.source, f"unknown footprint library '{lib}'")
        if fp in names:
            return Resolution(name, True, self.source)
        close = sorted(n for n in names if n.lower().startswith(fp.lower()[: max(4, len(fp) // 2)]))
        hint = f"did you mean {lib}:{close[0]}?" if close else None
        return Resolution(name, False, self.source, hint)
