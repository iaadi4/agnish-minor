"""Bill of materials as CSV (one row per unique part)."""

from __future__ import annotations

import csv
import io
from collections import defaultdict

from ..schemas import DISCLAIMER, DesignPlan

COLUMNS = [
    "Refs",
    "Qty",
    "Value",
    "MPN",
    "Manufacturer",
    "Footprint",
    "Description",
    "Datasheet",
    "Verified",
    "Min confidence",
    "Status",
    "Notes",
]


def _natural(ref: str) -> tuple[str, int]:
    head = ref.rstrip("0123456789")
    tail = ref[len(head) :]
    return (head, int(tail) if tail else 0)


def write_bom(plan: DesignPlan) -> str:
    groups: dict[tuple[str, str, str], list] = defaultdict(list)  # type: ignore[type-arg]
    for c in plan.components:
        if not c.in_bom:
            continue
        groups[(c.mpn, c.value or "", c.footprint or "")].append(c)
    rows = []
    for (mpn, value, fp), comps in groups.items():
        comps.sort(key=lambda c: _natural(c.ref_des))
        first = comps[0]
        notes = []
        if not first.verified and first.pins_known:
            notes.append(
                f"UNVERIFIED MPN/ratings: pins from KiCad symbol {first.kicad_symbol}; "
                "check the datasheet"
            )
        elif not first.verified:
            notes.append("UNVERIFIED: not in library, excluded from netlist")
        if first.datasheet_url is None:
            notes.append("no trusted datasheet URL")
        rows.append(
            [
                " ".join(c.ref_des for c in comps),
                str(len(comps)),
                value,
                mpn,
                first.manufacturer or "",
                fp,
                first.description,
                first.datasheet_url or "",
                "yes" if all(c.verified for c in comps) else "NO",
                f"{min(c.confidence for c in comps):.2f}",
                ",".join(sorted({c.status.value for c in comps})),
                "; ".join(notes),
            ]
        )
    rows.sort(key=lambda r: _natural(r[0].split()[0]))
    buf = io.StringIO()
    buf.write(f"# {DISCLAIMER}\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    w.writerows(rows)
    return buf.getvalue()
