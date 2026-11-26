"""Library/data-quality findings for the parts used in a plan."""

from __future__ import annotations

from ..library import FootprintResolver
from ..schemas import DesignPlan, Finding, FindingCategory, Severity


def library_findings(plan: DesignPlan, resolver: FootprintResolver | None = None) -> list[Finding]:
    resolver = resolver or FootprintResolver.detect()
    out: list[Finding] = []
    no_ds = []
    for c in plan.components:
        if not c.verified:
            continue
        if c.footprint:
            r = resolver.resolve(c.footprint)
            if not r.resolved:
                out.append(
                    Finding(
                        id=f"LIB-FOOTPRINT:{c.ref_des}",
                        rule="LIB-FOOTPRINT",
                        severity=Severity.ERROR,
                        category=FindingCategory.LIBRARY,
                        message=f"{c.ref_des} footprint {c.footprint} not found ({r.suggestion}).",
                        refs=[c.ref_des],
                    )
                )
        if c.in_bom and c.datasheet_url is None and c.origin != "synth":
            no_ds.append(c.ref_des)
        conf = c.key_specs.get("data_confidence")
        if conf == "low":
            out.append(
                Finding(
                    id=f"LIB-LOW-CONFIDENCE:{c.ref_des}",
                    rule="LIB-LOW-CONFIDENCE",
                    severity=Severity.WARNING,
                    category=FindingCategory.LIBRARY,
                    message=f"{c.ref_des} ({c.mpn}) library data is marked low confidence.",
                    refs=[c.ref_des],
                )
            )
    if no_ds:
        out.append(
            Finding(
                id="LIB-NO-DATASHEET:plan",
                rule="LIB-NO-DATASHEET",
                severity=Severity.INFO,
                category=FindingCategory.LIBRARY,
                message=(
                    f"No trusted datasheet URL for {', '.join(no_ds)} (KiCad metadata has none; "
                    "URLs are never guessed). Look these up from the manufacturer."
                ),
                refs=no_ds,
            )
        )
    return out
