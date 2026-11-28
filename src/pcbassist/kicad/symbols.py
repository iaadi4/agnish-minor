"""Search and resolve the stock KiCad symbol libraries (bundled index, KiCad 8.0.9).

This is the pipeline's source of truth for *what parts exist and what their pins are*:
the LLM may name any symbol here, and every ``REF.PIN`` it writes is checked against the
pin table returned by :meth:`SymbolIndex.get`. Nothing in this module is model-generated.
"""

from __future__ import annotations

import difflib
import gzip
import json
import os
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from ..paths import data_dir
from .pintags import PIN_TYPE_MAP, infer_tags


@dataclass(frozen=True)
class SymbolPin:
    number: str
    name: str
    kicad_type: str
    unit: int = 1

    @property
    def type(self) -> str:
        """Our PinType value (power_in, bidir, passive ...)."""
        return PIN_TYPE_MAP.get(self.kicad_type, "passive")

    @property
    def functions(self) -> list[str]:
        return infer_tags(self.name, self.type)


@dataclass(frozen=True)
class SymbolInfo:
    lib_id: str
    description: str
    keywords: str
    footprint: str
    fp_filters: tuple[str, ...]
    datasheet: str | None
    ref_prefix: str
    value: str
    units: int
    pins: tuple[SymbolPin, ...]
    power: bool = False

    @property
    def lib(self) -> str:
        return self.lib_id.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.lib_id.split(":", 1)[1]

    def pin(self, number: str) -> SymbolPin | None:
        return next((p for p in self.pins if p.number == number), None)

    def pin_table(self, limit: int | None = None) -> str:
        """``1=VDD/power_in 2=GND/power_in ...`` for prompts and tool results."""
        items = []
        for p in sorted(self.pins, key=lambda p: natural_key(p.number)):
            nm = "" if p.name in ("~", "") else p.name
            items.append(f"{p.number}={nm}/{p.type}" if nm else f"{p.number}({p.type})")
        if limit is not None and len(items) > limit:
            items = [*items[:limit], f"... ({len(self.pins) - limit} more)"]
        return " ".join(items)


@dataclass
class Resolution:
    info: SymbolInfo | None
    #: why it failed: no-library | no-symbol | found-elsewhere
    why: str | None = None
    candidates: list[str] = field(default_factory=list)

    def message(self, lib_id: str) -> str:
        if self.info is not None:
            return f"{lib_id}: ok"
        if self.why == "found-elsewhere":
            return f'"{lib_id}" does not exist, but the symbol is installed as: ' + ", ".join(
                self.candidates
            )
        where = f"; closest: {', '.join(self.candidates)}" if self.candidates else ""
        if self.why == "no-library":
            lib = lib_id.split(":", 1)[0]
            return f'no KiCad symbol library named "{lib}"{where}'
        return f'"{lib_id}" is not in that library{where}'


def natural_key(s: str) -> list[Any]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _wild(name: str) -> re.Pattern[str] | None:
    """KiCad names use a lowercase ``x`` for "any variant letter" (STM32F103C8Tx)."""
    if "x" not in name:
        return None
    parts = [
        re.escape(c.upper()) if c != "x" else "[A-Z0-9]" for c in re.sub(r"[^A-Za-z0-9]", "", name)
    ]
    return re.compile("".join(parts))


class SymbolIndex:
    def __init__(self, records: list[dict[str, Any]], source: str) -> None:
        self.source = source
        self._recs = {r["id"]: r for r in records}
        self._libs: dict[str, list[str]] = {}
        for rid in self._recs:
            lib, name = rid.split(":", 1)
            self._libs.setdefault(lib, []).append(name)
        self._by_name: dict[str, list[str]] = {}
        for rid in self._recs:
            self._by_name.setdefault(_norm(rid.split(":", 1)[1]), []).append(rid)
        self._wild: list[tuple[re.Pattern[str], str]] = []
        for rid in self._recs:
            w = _wild(rid.split(":", 1)[1])
            if w is not None:
                self._wild.append((w, rid))

    # -- loading --------------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: Path) -> SymbolIndex:
        payload = json.loads(gzip.decompress(path.read_bytes()))
        return cls(payload["symbols"], f"KiCad {payload.get('kicad_version', '?')} symbols")

    def __len__(self) -> int:
        return len(self._recs)

    def __contains__(self, lib_id: object) -> bool:
        return lib_id in self._recs

    @property
    def libraries(self) -> list[str]:
        return sorted(self._libs)

    # -- lookups --------------------------------------------------------------------------

    def get(self, lib_id: str) -> SymbolInfo | None:
        r = self._recs.get(lib_id)
        return _info(r) if r else None

    def resolve(self, lib_id: str) -> Resolution:
        info = self.get(lib_id)
        if info is not None:
            return Resolution(info)
        lib, _, name = lib_id.partition(":")
        if not name:
            name, lib = lib, ""
        same = self._by_name.get(_norm(name), [])
        if same:
            return Resolution(None, "found-elsewhere", same[:6])
        if lib not in self._libs:
            close = difflib.get_close_matches(lib, list(self._libs), n=3, cutoff=0.6)
            hits = [s.lib_id for s in self.search(name, limit=5)]
            return Resolution(None, "no-library", hits or [f"{c}:" for c in close])
        close = difflib.get_close_matches(name, self._libs[lib], n=5, cutoff=0.5)
        hits = [f"{lib}:{c}" for c in close] or [s.lib_id for s in self.search(name, limit=5)]
        return Resolution(None, "no-symbol", hits)

    def search(self, query: str, limit: int = 10, lib: str | None = None) -> list[SymbolInfo]:
        """Rank symbols for a part number or free-text query (exact names first)."""
        q = _norm(query)
        words = [w for w in re.findall(r"[a-z0-9][a-z0-9.+\-]*", query.lower()) if len(w) > 1]
        if not q:
            return []
        scores: dict[str, float] = {}

        def bump(rid: str, s: float) -> None:
            if lib and not rid.startswith(lib + ":"):
                return
            if s > scores.get(rid, 0):
                scores[rid] = s

        for rid in self._by_name.get(q, []):
            bump(rid, 100)
        for pat, rid in self._wild:
            if pat.fullmatch(q):
                bump(rid, 95)
            elif len(q) >= 5 and pat.match(q):
                bump(rid, 85)  # MPN with an ordering suffix (…TR, …-REEL)
        if len(q) >= 3:
            for nm, rids in self._by_name.items():
                if nm == q:
                    continue
                if len(q) >= 5 and q.startswith(nm) and len(nm) >= 5:
                    for rid in rids:
                        bump(rid, 80 + 10 * len(nm) / len(q))
                elif nm.startswith(q):
                    for rid in rids:
                        bump(rid, 70 - min(len(nm) - len(q), 20) / 2)
                elif q in nm:
                    for rid in rids:
                        bump(rid, 50)
        if words and (not scores or max(scores.values()) < 60):
            for rid, r in self._recs.items():
                hay = f"{rid} {r['d']} {r['k']}".lower()
                n = sum(1 for w in words if w in hay)
                if n:
                    bump(rid, 10 + 30 * n / len(words))
        ranked = sorted(scores, key=lambda rid: (-scores[rid], len(rid), rid))
        return [_info(self._recs[rid]) for rid in ranked[:limit]]


def _info(r: dict[str, Any]) -> SymbolInfo:
    return SymbolInfo(
        lib_id=r["id"],
        description=r.get("d", ""),
        keywords=r.get("k", ""),
        footprint=r.get("f", ""),
        fp_filters=tuple(r.get("ff", [])),
        datasheet=r.get("ds") or None,
        ref_prefix=r.get("r", "U"),
        value=r.get("v", ""),
        units=int(r.get("u", 1)),
        pins=tuple(SymbolPin(str(p[0]), str(p[1]), str(p[2]), int(p[3])) for p in r.get("p", [])),
        power=bool(r.get("pw", False)),
    )


@cache
def default_symbol_index() -> SymbolIndex:
    """The bundled index (``PCBASSIST_SYMBOL_INDEX`` overrides the path)."""
    env = os.environ.get("PCBASSIST_SYMBOL_INDEX")
    path = Path(env) if env else data_dir() / "kicad" / "symbol_index.json.gz"
    return SymbolIndex.from_file(path)
