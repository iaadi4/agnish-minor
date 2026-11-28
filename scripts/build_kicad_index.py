#!/usr/bin/env python3
"""Build the bundled KiCad library index from an installed KiCad 8 library tree.

    uv run python scripts/build_kicad_index.py [--root ~/.cache/pcbassist/kicad/8.0.9]

``--root`` must contain ``symbols/*.kicad_sym`` and ``footprints/*.pretty``. Get it from a
KiCad install, or from the official Docker image with ``scripts/fetch-kicad-libs``.

Writes ``data/kicad/symbol_index.json.gz`` and ``data/kicad/footprint_geom.json.gz``. The
KiCad libraries are CC-BY-SA 4.0 with the KiCad library exception (see data/kicad/NOTICE).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcbassist.kicad.libindex import parse_footprint_record, parse_symbol_lib  # noqa: E402

OUT = ROOT / "data" / "kicad"


def _sym(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    return parse_symbol_lib(p.read_text(encoding="utf-8"), p.stem)


def _fp_lib(path: str) -> dict[str, dict[str, Any]]:
    lib = Path(path)
    name = lib.name[: -len(".pretty")]
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(lib.glob("*.kicad_mod")):
        try:
            out[f"{name}:{f.stem}"] = parse_footprint_record(
                f.read_text(encoding="utf-8"), f"{name}:{f.stem}"
            )
        except Exception as e:  # pragma: no cover - reported, not fatal
            print(f"skip {name}:{f.stem}: {e}", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path.home() / ".cache/pcbassist/kicad/8.0.9")
    ap.add_argument("--version", default="8.0.9")
    args = ap.parse_args()
    sym_dir, fp_dir = args.root / "symbols", args.root / "footprints"
    if not sym_dir.is_dir() or not fp_dir.is_dir():
        print(f"{args.root} needs symbols/ and footprints/ (see scripts/fetch-kicad-libs)")
        return 1
    with cf.ProcessPoolExecutor() as ex:
        sym_files = sorted(str(p) for p in sym_dir.glob("*.kicad_sym"))
        symbols = [s for batch in ex.map(_sym, sym_files) for s in batch]
        fp_libs = sorted(str(p) for p in fp_dir.glob("*.pretty"))
        footprints: dict[str, dict[str, Any]] = {}
        for d in ex.map(_fp_lib, fp_libs):
            footprints.update(d)
    symbols.sort(key=lambda s: s["id"])
    meta = {"kicad_version": args.version, "license": "CC-BY-SA-4.0 WITH KiCad-libraries-exception"}
    OUT.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("symbol_index.json.gz", {**meta, "symbols": symbols}),
        ("footprint_geom.json.gz", {**meta, "footprints": dict(sorted(footprints.items()))}),
    ):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        (OUT / name).write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
        print(f"{name}: {len(raw) / 1e6:.1f} MB raw, {(OUT / name).stat().st_size / 1e6:.1f} MB gz")
    print(f"{len(symbols)} symbols, {len(footprints)} footprints")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
