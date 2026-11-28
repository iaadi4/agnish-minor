#!/usr/bin/env python3
"""Build ``data/components_seed.json`` from ``data/parts_curated.toml`` + official KiCad libraries.

Provenance rules (see PLAN.md):
* Pin numbers / names / electrical types come from the KiCad symbol library
  (``kicad-symbols`` @ SYMBOL_REF). Generic connectors derive pins from footprint pads.
* Datasheet URLs come only from the KiCad symbol "Datasheet" property.
* Footprints come from ``kicad-footprints`` @ FOOTPRINT_REF (KiCad 8 format, readable by
  KiCad 8/9/10) and are vendored into ``data/kicad/footprints/`` for PCB export.
* Every symbol pin number must exist as a pad in the footprint, or the build fails.

Usage:  uv run python scripts/build_seed.py [--index]   (``--index`` rebuilds the full
footprint name index, ~300 API calls).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import re
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcbassist.kicad.pintags import (  # noqa: E402
    PIN_TYPE_MAP,
    expand_pin_number,
    infer_tags,
    normalize_pin_name,
)
from pcbassist.kicad.sexpr import SExpr, Symbol, child, children, parse, properties, value, walk  # noqa: E402
from pcbassist.schemas import LibraryPart  # noqa: E402

SYMBOL_REF = "master"
FOOTPRINT_REF = "8.0.9"
GITLAB_RAW = "https://gitlab.com/kicad/libraries/{repo}/-/raw/{ref}/{path}"
GITLAB_API = "https://gitlab.com/api/v4/projects/kicad%2Flibraries%2F{repo}/repository/tree"
CACHE = ROOT / ".cache" / "kicad"
FP_VENDOR = ROOT / "data" / "kicad" / "footprints"

# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------


def fetch(repo: str, ref: str, path: str) -> str:
    cache = CACHE / repo / ref / path
    if cache.exists():
        return cache.read_text()
    url = GITLAB_RAW.format(repo=repo, ref=ref, path=urllib.parse.quote(path))
    with urllib.request.urlopen(url, timeout=60) as r:
        text: str = r.read().decode("utf-8")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text)
    return text


def tree(repo: str, ref: str, path: str = "") -> list[str]:
    names: list[str] = []
    page = 1
    while True:
        q = urllib.parse.urlencode({"ref": ref, "per_page": 100, "page": page, "path": path})
        with urllib.request.urlopen(f"{GITLAB_API.format(repo=repo)}?{q}", timeout=60) as r:
            data = json.load(r)
        names += [d["name"] for d in data]
        if len(data) < 100:
            return names
        page += 1


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------


def load_symbol(lib: str, name: str) -> list[SExpr]:
    text = fetch("kicad-symbols", SYMBOL_REF, f"{lib}.kicad_symdir/{name}.kicad_sym")
    root = parse(text)
    for sym in children(root, "symbol"):
        if sym[1] == name:
            return sym
    raise KeyError(f"symbol {lib}:{name} not found in its file")


def symbol_pins_and_props(lib: str, name: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    sym = load_symbol(lib, name)
    props = properties(sym)
    ext = child(sym, "extends")
    if ext is not None:
        parent_pins, parent_props = symbol_pins_and_props(lib, str(ext[1]))
        merged = {**parent_props, **props}
        return parent_pins, merged
    pins: dict[str, dict[str, Any]] = {}
    for pin in walk(sym, "pin"):
        if len(pin) < 3 or not isinstance(pin[1], Symbol):
            continue
        num = value(child(pin, "number"))
        nm = value(child(pin, "name"))
        if not isinstance(num, str):
            continue
        ptype = str(pin[1])
        for n in expand_pin_number(num):
            pins.setdefault(
                n, {"number": n, "name": str(nm) if nm is not None else n, "kicad_type": ptype}
            )
    return list(pins.values()), props


# ---------------------------------------------------------------------------
# Footprints
# ---------------------------------------------------------------------------


def footprint_info(fp_name: str) -> dict[str, Any]:
    lib, name = fp_name.split(":", 1)
    path = f"{lib}.pretty/{name}.kicad_mod"
    text = fetch("kicad-footprints", FOOTPRINT_REF, path)
    vend = FP_VENDOR / path
    vend.parent.mkdir(parents=True, exist_ok=True)
    vend.write_text(text)
    root = parse(text)
    pads: list[tuple[str, str, float, float]] = []
    for pad in children(root, "pad"):
        num = pad[1]
        kind = str(pad[2])
        at = child(pad, "at")
        x = float(at[1]) if at else 0.0  # type: ignore[arg-type]
        y = float(at[2]) if at else 0.0  # type: ignore[arg-type]
        pads.append((str(num), kind, x, y))
    xs: list[float] = []
    ys: list[float] = []
    for tag in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
        for g in children(root, tag):
            layer = value(child(g, "layer"))
            if layer != "F.CrtYd":
                continue
            if tag == "fp_circle":
                c, e = child(g, "center"), child(g, "end")
                if c and e:
                    r = math.dist((float(c[1]), float(c[2])), (float(e[1]), float(e[2])))  # type: ignore[arg-type]
                    xs += [float(c[1]) - r, float(c[1]) + r]  # type: ignore[arg-type]
                    ys += [float(c[2]) - r, float(c[2]) + r]  # type: ignore[arg-type]
                continue
            for pt_tag in ("start", "end", "mid"):
                pt = child(g, pt_tag)
                if pt:
                    xs.append(float(pt[1]))  # type: ignore[arg-type]
                    ys.append(float(pt[2]))  # type: ignore[arg-type]
            pts = child(g, "pts")
            if pts:
                for xy in children(pts, "xy"):
                    xs.append(float(xy[1]))  # type: ignore[arg-type]
                    ys.append(float(xy[2]))  # type: ignore[arg-type]
    numbered = [(n, k, x, y) for n, k, x, y in pads if n != "" and k != "np_thru_hole"]
    pitch: float | None = None
    for i, a in enumerate(numbered):
        for b in numbered[i + 1 :]:
            if a[0] == b[0]:
                continue
            d = math.dist((a[2], a[3]), (b[2], b[3]))
            if d > 0 and (pitch is None or d < pitch):
                pitch = d
    return {
        "name": fp_name,
        "pad_numbers": sorted({n for n, *_ in numbered}, key=_natkey),
        "courtyard_w_mm": round(max(xs) - min(xs), 3) if xs else None,
        "courtyard_h_mm": round(max(ys) - min(ys), 3) if ys else None,
        "smd": all(k == "smd" for _, k, *_ in numbered) if numbered else False,
        "min_pad_pitch_mm": round(pitch, 3) if pitch else None,
    }


def _natkey(s: str) -> list[Any]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_part(spec: dict[str, Any]) -> dict[str, Any]:
    sym_ref: str = spec["kicad_symbol"]
    datasheet: str | None = None
    if sym_ref == "generic":
        fp_name = spec["footprint"]
        fpi = footprint_info(fp_name)
        names: dict[str, str] = spec.get("pin_names", {})
        raw_pins = [
            {
                "number": n,
                "name": names.get(n, "MP" if n == "MP" else f"Pin_{n}"),
                "kicad_type": "passive",
            }
            for n in fpi["pad_numbers"]
        ]
        pin_source = f"kicad-footprints@{FOOTPRINT_REF}:{fp_name} pads"
    else:
        lib, name = sym_ref.split(":", 1)
        raw_pins, props = symbol_pins_and_props(lib, name)
        fp_name = spec.get("footprint") or props.get("Footprint", "")
        if not fp_name:
            raise ValueError(f"{spec['mpn']}: symbol {sym_ref} has no default footprint; set one")
        fpi = footprint_info(fp_name)
        ds = props.get("Datasheet", "").strip()
        datasheet = ds if ds.startswith("http") else None
        pin_source = f"kicad-symbols@{SYMBOL_REF}:{sym_ref}"
        pin_map: dict[str, str] = spec.get("pin_map", {})
        for p in raw_pins:
            if p["number"] in pin_map:
                p["number"] = pin_map[p["number"]]
        if pin_map:
            pin_source += f" (pin_map {pin_map})"
        missing = [p["number"] for p in raw_pins if p["number"] not in fpi["pad_numbers"]]
        if missing:
            raise ValueError(
                f"{spec['mpn']}: symbol pins {missing} have no pad in footprint {fp_name} "
                f"(pads: {fpi['pad_numbers']})"
            )

    extra_tags: dict[str, list[str]] = spec.get("pin_tags", {})
    pins = []
    for p in raw_pins:
        p["name"] = normalize_pin_name(p["name"])
    for p in sorted(raw_pins, key=lambda p: _natkey(p["number"])):
        ptype = PIN_TYPE_MAP[p["kicad_type"]]
        tags = infer_tags(p["name"], ptype)
        if p["name"] == "MP":
            tags.append("mounting")
        for t in extra_tags.get(p["name"], []):
            if t not in tags:
                tags.append(t)
        pins.append({"number": p["number"], "name": p["name"], "type": ptype, "functions": tags})
    if spec.get("pin_tags"):
        known = {p["name"] for p in pins}
        unknown = sorted(set(spec["pin_tags"]) - known)
        if unknown:
            raise ValueError(
                f"{spec['mpn']}: pin_tags for unknown pin names {unknown}; have {sorted(known)}"
            )

    supply = spec.get("supply")
    ref_prefix = spec.get("ref_prefix") or _default_prefix(spec["category"])
    part = {
        "mpn": spec["mpn"],
        "manufacturer": spec["manufacturer"],
        "category": spec["category"],
        "description": spec["description"],
        "package": fp_name.split(":", 1)[1],
        "footprint": fp_name,
        "footprint_info": fpi,
        "ref_prefix": ref_prefix,
        "value": spec.get("value"),
        "pins": pins,
        "supply_range": {"min_v": supply[0], "max_v": supply[1]} if supply else None,
        "current": {
            "typ_ma": spec.get("current_typ_ma"),
            "max_ma": spec.get("current_max_ma"),
            "sleep_ua": spec.get("sleep_ua"),
        },
        "key_specs": spec.get("specs", {}),
        "tags": spec.get("tags", []),
        "datasheet_url": datasheet,
        "kicad_symbol": None if sym_ref == "generic" else sym_ref,
        "pin_source": pin_source,
        "in_bom": spec.get("in_bom", True),
        "data_confidence": spec.get("data_confidence", "medium"),
        "notes": spec.get("notes"),
    }
    return LibraryPart.model_validate(part).model_dump(mode="json")


def _default_prefix(category: str) -> str:
    return {
        "resistor": "R",
        "capacitor": "C",
        "inductor": "L",
        "ferrite": "FB",
        "led": "D",
        "diode": "D",
        "tvs": "D",
        "esd": "D",
        "mosfet": "Q",
        "crystal": "Y",
        "switch": "SW",
        "relay": "K",
        "fuse": "F",
        "battery_holder": "BT",
        "mechanical": "H",
        "test_point": "TP",
    }.get(category, "J" if category.startswith("connector") else "U")


def build_index() -> dict[str, list[str]]:
    libs = [n for n in tree("kicad-footprints", FOOTPRINT_REF) if n.endswith(".pretty")]
    out: dict[str, list[str]] = {}

    def one(lib: str) -> tuple[str, list[str]]:
        names = tree("kicad-footprints", FOOTPRINT_REF, lib)
        return lib[: -len(".pretty")], sorted(
            n[: -len(".kicad_mod")] for n in names if n.endswith(".kicad_mod")
        )

    with cf.ThreadPoolExecutor(6) as ex:
        for lib, names in ex.map(one, libs):
            out[lib] = names
    return dict(sorted(out.items()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", action="store_true", help="also rebuild the footprint name index")
    args = ap.parse_args()

    curated = tomllib.loads((ROOT / "data" / "parts_curated.toml").read_text())["part"]
    mpns = [p["mpn"] for p in curated]
    dupes = {m for m in mpns if mpns.count(m) > 1}
    if dupes:
        print(f"duplicate MPNs: {dupes}", file=sys.stderr)
        return 1

    errors: list[str] = []
    parts: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(build_part, spec): spec["mpn"] for spec in curated}
        for fut in cf.as_completed(futs):
            try:
                parts.append(fut.result())
            except Exception as e:  # report all problems in one pass
                errors.append(f"{futs[fut]}: {type(e).__name__}: {e}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    order = {m: i for i, m in enumerate(mpns)}
    parts.sort(key=lambda p: order[p["mpn"]])
    seed = {
        "generated_by": "scripts/build_seed.py",
        "sources": {
            "curated": "data/parts_curated.toml",
            "kicad_symbols": f"https://gitlab.com/kicad/libraries/kicad-symbols @ {SYMBOL_REF}",
            "kicad_footprints": (
                f"https://gitlab.com/kicad/libraries/kicad-footprints @ {FOOTPRINT_REF}"
            ),
        },
        "license_note": "Pin tables and footprints derived from the KiCad libraries (CC-BY-SA 4.0 "
        "with design exception).",
        "parts": parts,
    }
    (ROOT / "data" / "components_seed.json").write_text(json.dumps(seed, indent=1) + "\n")
    print(f"wrote {len(parts)} parts")

    if args.index:
        idx = build_index()
        (ROOT / "data" / "kicad").mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "kicad" / "footprint_index.json").write_text(
            json.dumps({"ref": FOOTPRINT_REF, "libraries": idx}, separators=(",", ":")) + "\n"
        )
        print(f"indexed {sum(len(v) for v in idx.values())} footprints in {len(idx)} libraries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
