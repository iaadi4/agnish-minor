"""The stock KiCad footprint libraries: names, pads and courtyards (bundled index).

Resolution order for a ``Lib:Name`` footprint's *file* (needed to embed it in a board):

1. vendored copy in ``data/kicad/footprints`` (the curated library's parts),
2. a local KiCad footprint tree (``PCBASSIST_KICAD_FOOTPRINT_DIR``, ``KICAD8_FOOTPRINT_DIR``,
   standard install paths, or ``~/.cache/pcbassist/kicad/<ver>/footprints`` written by
   ``scripts/fetch-kicad-libs``),
3. a footprint rebuilt from the bundled geometry index (pads + courtyard only; KiCad's
   *Update Footprints from Library* restores silkscreen and fab graphics).
"""

from __future__ import annotations

import difflib
import gzip
import json
import os
from functools import cache
from pathlib import Path
from typing import Any

from ..paths import data_dir
from .footprint import FootprintGeom, PadGeom

_STD = [
    "/usr/share/kicad/footprints",
    "/usr/local/share/kicad/footprints",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    "C:/Program Files/KiCad/8.0/share/kicad/footprints",
]


class FootprintIndex:
    def __init__(self, records: dict[str, dict[str, Any]], source: str) -> None:
        self._recs = records
        self.source = source
        self._libs: dict[str, list[str]] = {}
        self._by_name: dict[str, list[str]] = {}
        for fid in records:
            lib, name = fid.split(":", 1)
            self._libs.setdefault(lib, []).append(name)
            self._by_name.setdefault(name.lower(), []).append(fid)

    @classmethod
    def from_file(cls, path: Path) -> FootprintIndex:
        payload = json.loads(gzip.decompress(path.read_bytes()))
        return cls(payload["footprints"], f"KiCad {payload.get('kicad_version', '?')} footprints")

    def __contains__(self, name: object) -> bool:
        return name in self._recs

    def __len__(self) -> int:
        return len(self._recs)

    def pad_numbers(self, name: str) -> set[str] | None:
        r = self._recs.get(name)
        if r is None:
            return None
        return {str(p[0]) for p in r["p"] if p[0] != "" and p[1] != "np_thru_hole"}

    def is_smd(self, name: str) -> bool | None:
        g = self.geom(name)
        return g.smd if g else None

    def geom(self, name: str) -> FootprintGeom | None:
        r = self._recs.get(name)
        if r is None:
            return None
        pads = tuple(
            PadGeom(
                number=str(p[0]),
                kind=str(p[1]),
                shape=str(p[2]),
                x=float(p[3]),
                y=float(p[4]),
                w=float(p[5]),
                h=float(p[6]),
                rot=float(p[7]),
                drill=float(p[8]) if p[8] is not None else None,
            )
            for p in r["p"]
        )
        cy = tuple(float(v) for v in r["c"])
        rects = tuple(tuple(float(v) for v in rr) for rr in r.get("rr", [])) or (cy,)
        return FootprintGeom(
            name=name,
            path=Path(f"<index>/{name}"),
            courtyard=(cy[0], cy[1], cy[2], cy[3]),
            pads=pads,
            has_courtyard=bool(r.get("hc", True)),
            rects=tuple((r4[0], r4[1], r4[2], r4[3]) for r4 in rects),
        )

    def nearest(self, name: str, n: int = 4) -> list[str]:
        """Closest installed ids: same name in another library first, then fuzzy names."""
        lib, _, fp = name.partition(":")
        if not fp:
            fp, lib = lib, ""
        same = [f for f in self._by_name.get(fp.lower(), []) if f != name]
        if same:
            return same[:n]
        if lib in self._libs:
            return [f"{lib}:{c}" for c in difflib.get_close_matches(fp, self._libs[lib], n, 0.4)]
        pool = list(self._recs)
        return difflib.get_close_matches(name, pool, n, 0.5)

    def search(self, query: str, limit: int = 10) -> list[str]:
        words = query.lower().split()
        hits = []
        for fid, r in self._recs.items():
            hay = f"{fid} {r.get('d', '')} {r.get('t', '')}".lower()
            if all(w in hay for w in words):
                hits.append(fid)
        hits.sort(key=lambda f: (len(f), f))
        return hits[:limit]


@cache
def default_footprint_index() -> FootprintIndex:
    env = os.environ.get("PCBASSIST_FOOTPRINT_INDEX")
    path = Path(env) if env else data_dir() / "kicad" / "footprint_geom.json.gz"
    return FootprintIndex.from_file(path)


@cache
def local_footprint_root() -> Path | None:
    """A local KiCad footprint tree with full ``.kicad_mod`` files, if one exists.

    ``PCBASSIST_KICAD_FOOTPRINT_DIR=none`` disables the lookup (hermetic tests).
    """
    if os.environ.get("PCBASSIST_KICAD_FOOTPRINT_DIR", "").lower() == "none":
        return None
    for var in ("PCBASSIST_KICAD_FOOTPRINT_DIR", "KICAD8_FOOTPRINT_DIR"):
        v = os.environ.get(var)
        if v and Path(v).is_dir():
            return Path(v)
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pcbassist"
    candidates = [Path(p) for p in _STD] + sorted(
        (cache_root / "kicad").glob("8.*/footprints"), reverse=True
    )
    for p in candidates:
        if p.is_dir() and any(p.glob("Resistor_SMD.pretty")):
            return p
    return None


def local_footprint_file(name: str) -> Path | None:
    root = local_footprint_root()
    if root is None:
        return None
    lib, _, fp = name.partition(":")
    p = root / f"{lib}.pretty" / f"{fp}.kicad_mod"
    return p if p.is_file() else None
