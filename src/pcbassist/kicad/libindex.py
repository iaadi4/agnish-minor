"""Parsers that turn the official KiCad libraries into a compact, searchable index.

``scripts/build_kicad_index.py`` runs these over a KiCad 8 install and writes
``data/kicad/symbol_index.json.gz`` and ``data/kicad/footprint_geom.json.gz``. The index
lets the pipeline search *every* stock symbol, read its real pins, and check that a symbol's
pins exist as pads of a footprint, with no KiCad installed at run time.

Symbol record (keys kept short, the file holds ~20k of them)::

    {"id": "Lib:Name", "d": description, "k": keywords, "f": default footprint,
     "ff": [footprint filters], "ds": datasheet, "r": reference prefix, "v": value,
     "pw": true for power symbols, "u": unit count,
     "p": [[number, name, kicad_type, unit], ...]}

Footprint record::

    {"c": [x0, y0, x1, y1] courtyard bbox, "hc": has courtyard, "rr": [[rect], ...],
     "a": attr (smd | through_hole | ""), "p": [[num, kind, shape, x, y, w, h, rot, drill]]}
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .footprint import parse_footprint
from .pintags import expand_pin_number, normalize_pin_name
from .sexpr import SExpr, Symbol, child, children, head, parse, properties, value

_UNIT = re.compile(r"^(.*)_(\d+)_(\d+)$")


def _r(v: float) -> float:
    return round(v, 4)


def _symbol_pins(sym: list[SExpr], name: str) -> tuple[list[list[Any]], int]:
    """Pins of one symbol definition (not following ``extends``), and its unit count."""
    pins: dict[str, list[Any]] = {}
    units: set[int] = set()
    for sub in children(sym, "symbol"):
        sub_name = str(value(sub, 1) or "")
        m = _UNIT.match(sub_name)
        unit, style = (int(m.group(2)), int(m.group(3))) if m else (1, 1)
        if style > 1:
            continue  # de Morgan alternate: same pins as style 1
        if unit > 0:
            units.add(unit)
        for pin in children(sub, "pin"):
            if len(pin) < 3 or not isinstance(pin[1], Symbol):
                continue
            num = value(child(pin, "number"))
            nm = value(child(pin, "name"))
            if num is None:
                continue
            for n in expand_pin_number(str(num)):
                pins.setdefault(
                    n,
                    [n, normalize_pin_name(str(nm)) if nm is not None else n, str(pin[1]), unit],
                )
    return list(pins.values()), max(len(units), 1)


def parse_symbol_lib(text: str, lib: str) -> list[dict[str, Any]]:
    """Every symbol in one ``.kicad_sym`` file, with ``extends`` resolved inside the file."""
    root = parse(text)
    raw: dict[str, list[SExpr]] = {}
    for sym in children(root, "symbol"):
        raw[str(value(sym, 1))] = sym

    def resolve(name: str, depth: int = 0) -> tuple[list[list[Any]], int, dict[str, str], bool]:
        sym = raw[name]
        props = properties(sym)
        power = child(sym, "power") is not None
        ext = child(sym, "extends")
        if ext is not None and depth < 8 and str(ext[1]) in raw:
            ppins, punits, pprops, ppower = resolve(str(ext[1]), depth + 1)
            return ppins, punits, {**pprops, **props}, power or ppower
        pins, units = _symbol_pins(sym, name)
        return pins, units, props, power

    out: list[dict[str, Any]] = []
    for name in raw:
        pins, units, props, power = resolve(name)
        fp_filters = props.get("ki_fp_filters", "")
        rec: dict[str, Any] = {
            "id": f"{lib}:{name}",
            "d": props.get("Description", props.get("ki_description", "")).strip(),
            "k": props.get("ki_keywords", "").strip(),
            "f": props.get("Footprint", "").strip(),
            "ff": fp_filters.split() if fp_filters else [],
            "ds": _datasheet(props.get("Datasheet", "")),
            "r": props.get("Reference", "U").strip() or "U",
            "v": props.get("Value", name).strip(),
            "u": units,
            "p": pins,
        }
        if power:
            rec["pw"] = True
        out.append(rec)
    return out


def _datasheet(s: str) -> str:
    s = s.strip()
    return s if s.startswith(("http://", "https://")) else ""


def _attr(fp: SExpr) -> str:
    a = child(fp, "attr")
    return str(value(a, 1) or "") if a else ""


def parse_footprint_record(text: str, name: str) -> dict[str, Any]:
    geom = parse_footprint(text, name, Path(name))
    fp = parse(text)
    pads = [
        [
            p.number,
            p.kind,
            p.shape,
            _r(p.x),
            _r(p.y),
            _r(p.w),
            _r(p.h),
            _r(p.rot),
            _r(p.drill) if p.drill is not None else None,
        ]
        for p in geom.pads
    ]
    rec: dict[str, Any] = {
        "c": [_r(v) for v in geom.courtyard],
        "hc": geom.has_courtyard,
        "a": _attr(fp),
        "p": pads,
    }
    if geom.rects and tuple(geom.rects) != (geom.courtyard,):
        rec["rr"] = [[_r(v) for v in r] for r in geom.rects]
    descr = value(child(fp, "descr"))
    if isinstance(descr, str) and descr:
        rec["d"] = descr[:160]
    tags = value(child(fp, "tags"))
    if isinstance(tags, str) and tags:
        rec["t"] = tags[:120]
    return rec


def is_footprint(x: SExpr) -> bool:
    return head(x) in ("footprint", "module")
