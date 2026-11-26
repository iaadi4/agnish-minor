"""Exporters: KiCad netlist/project/board/schematic, BOM CSV, markdown report, DRC."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..checks import run_checks
from ..schemas import DesignPlan
from .bom import write_bom
from .drc import DrcRun, find_kicad_cli, run_drc
from .kicad_pcb import write_pcb
from .kicad_pro import write_project
from .kicad_sch import write_schematic
from .netlist import write_netlist
from .report import write_report

FORMATS = ("kicad", "bom", "report")


@dataclass
class ExportResult:
    files: list[Path] = field(default_factory=list)
    drc: DrcRun | None = None


def export_plan(
    plan: DesignPlan,
    out_dir: Path,
    *,
    formats: tuple[str, ...] = FORMATS,
    name: str = "board",
    drc: bool = True,
) -> ExportResult:
    """Write the requested formats. With ``drc``, KiCad DRC findings are merged into ``plan``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    res = ExportResult()
    date = plan.created_at.date().isoformat()
    if "kicad" in formats:
        for fname, text in (
            (f"{name}.net", write_netlist(plan, date)),
            (f"{name}.kicad_pro", write_project(plan, name)),
            (f"{name}.kicad_sch", write_schematic(plan, name)),
            (f"{name}.kicad_pcb", write_pcb(plan, name)),
        ):
            p = out_dir / fname
            p.write_text(text)
            res.files.append(p)
        if drc:
            res.drc = run_drc(out_dir / f"{name}.kicad_pcb")
            run_checks(plan, drc_report=res.drc.report)
            if plan.check_summary is not None:
                plan.check_summary.kicad_drc_note = res.drc.note
            if res.drc.report is not None:
                res.files.append(res.drc.report)
    if "bom" in formats:
        p = out_dir / "bom.csv"
        p.write_text(write_bom(plan))
        res.files.append(p)
    if "report" in formats:
        p = out_dir / "report.md"
        names = [f.name for f in res.files] + ["report.md"]
        p.write_text(write_report(plan, names))
        res.files.append(p)
    return res


__all__ = [
    "FORMATS",
    "DrcRun",
    "ExportResult",
    "export_plan",
    "find_kicad_cli",
    "run_drc",
    "write_bom",
    "write_netlist",
    "write_pcb",
    "write_project",
    "write_report",
    "write_schematic",
]
