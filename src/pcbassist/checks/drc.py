"""Parse a ``kicad-cli pcb drc --format json`` report into findings."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..schemas import Finding, FindingCategory, FindingSource, Severity

#: Violation types that are expected on an unrouted, auto-placed board and are
#: therefore reported as INFO with an explanation rather than as design errors.
EXPECTED = {
    "unconnected_items": "routing is a manual step; every ratsnest line is reported",
    "lib_footprint_issues": "the board was checked without the KiCad footprint library tables",
    "lib_footprint_mismatch": "footprints were embedded from the vendored KiCad 8 library",
    "silk_overlap": "silkscreen reference positions come from the library footprints",
    "silk_over_copper": "silkscreen reference positions come from the library footprints",
    "silk_edge_clearance": "silkscreen near the edge on edge-mounted connectors",
    "text_height": "library footprint text",
    "text_thickness": "library footprint text",
}


_OWNER = re.compile(r" of ([A-Z]{1,4}[0-9]{1,4})\b")


def _single_footprint(v: dict[str, Any]) -> str | None:
    """Ref of the one footprint all items of a violation belong to, if any."""
    owners = set()
    for item in v.get("items", []):
        m = _OWNER.search(str(item.get("description", "")))
        if m is None:
            return None
        owners.add(m.group(1))
    return owners.pop() if len(owners) == 1 else None


def _sev(s: str) -> Severity:
    return {"error": Severity.ERROR, "warning": Severity.WARNING}.get(s, Severity.INFO)


def parse_drc(data: dict[str, Any]) -> list[Finding]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for key in ("violations", "unconnected_items", "schematic_parity"):
        for v in data.get(key, []) or []:
            vtype = str(v.get("type", key))
            sev = str(v.get("severity", "error"))
            if key == "violations" and sev == "error" and _single_footprint(v):
                # e.g. hole clearance between two pads of the same library footprint
                sev = "footprint"
            groups[(vtype, sev)].append(v)
    out: list[Finding] = []
    for (vtype, sev), items in sorted(groups.items()):
        expected = EXPECTED.get(vtype)
        details = []
        for v in items[:4]:
            desc = "; ".join(str(i.get("description", "")) for i in v.get("items", [])[:2])
            details.append(f"{v.get('description', vtype)} [{desc}]")
        more = f" (+{len(items) - 4} more)" if len(items) > 4 else ""
        msg = f"KiCad DRC {vtype}: {len(items)} item(s). " + " | ".join(details) + more
        if expected:
            msg += f" Expected here: {expected}."
        severity = Severity.INFO if expected else _sev(sev)
        if sev == "footprint":
            owners = sorted({o for v in items if (o := _single_footprint(v))})
            msg += (
                f" All inside the KiCad library footprint(s) of {', '.join(owners)}: check "
                "that your fab accepts them or adapt the footprint."
            )
            severity = Severity.WARNING
        out.append(
            Finding(
                id=f"DRC-{vtype.upper()}:{sev}",
                rule=f"DRC-{vtype.upper()}",
                severity=severity,
                category=FindingCategory.DRC,
                message=msg,
                source=FindingSource.KICAD,
            )
        )
    return out


def load_drc_report(path: Path) -> list[Finding]:
    return parse_drc(json.loads(path.read_text()))
