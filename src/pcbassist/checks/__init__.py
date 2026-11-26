"""Deterministic design checks: ERC, power budget, DFM/placement, library, KiCad DRC."""

from __future__ import annotations

from pathlib import Path

from ..fab import load_fab_profile
from ..schemas import CheckSummary, DesignPlan, Finding
from .dfm import run_dfm
from .drc import load_drc_report, parse_drc
from .erc import run_erc
from .library import library_findings
from .power import analyze_power, power_findings

CHECK_PREFIXES = ("ERC-", "PWR-", "DFM-", "LIB-NO-", "LIB-FOOTPRINT", "LIB-LOW", "DRC-")


def run_checks(plan: DesignPlan, *, drc_report: Path | None = None) -> list[Finding]:
    """Run every check, replace previous check findings, update ``check_summary``."""
    fab_name = plan.constraints.mechanical.fab_profile if plan.constraints else "generic_2layer"
    fab = load_fab_profile(fab_name)
    new: list[Finding] = []
    new += run_erc(plan)
    new += power_findings(plan)
    new += run_dfm(plan, fab)
    new += library_findings(plan)
    drc_note = "KiCad DRC not run (kicad-cli not available or not requested)."
    ran = False
    if drc_report is not None and drc_report.exists():
        new += load_drc_report(drc_report)
        ran = True
        drc_note = f"KiCad DRC report merged from {drc_report.name}."
    keep = [f for f in plan.findings if not f.id.startswith(CHECK_PREFIXES)]
    seen: set[str] = set()
    merged: list[Finding] = []
    for f in keep + new:
        if f.id in seen:
            continue
        seen.add(f.id)
        merged.append(f)
    order = {"error": 0, "warning": 1, "info": 2}
    merged.sort(key=lambda f: (order[f.severity.value], f.category.value, f.id))
    plan.findings = merged
    plan.check_summary = CheckSummary.from_findings(merged, kicad_drc_ran=ran, note=drc_note)
    return new


__all__ = [
    "analyze_power",
    "library_findings",
    "parse_drc",
    "power_findings",
    "run_checks",
    "run_dfm",
    "run_erc",
]
