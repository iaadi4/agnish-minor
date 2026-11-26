"""Run KiCad's DRC through ``kicad-cli`` when it is available."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DrcRun:
    ran: bool
    report: Path | None
    note: str


def find_kicad_cli() -> str | None:
    """``PCBASSIST_KICAD_CLI`` (e.g. scripts/kicad-cli-docker) or ``kicad-cli`` on PATH."""
    env = os.environ.get("PCBASSIST_KICAD_CLI")
    if env:
        return env if Path(env).exists() or shutil.which(env) else None
    return shutil.which("kicad-cli")


def run_drc(pcb: Path, timeout: float = 300.0) -> DrcRun:
    cli = find_kicad_cli()
    if cli is None:
        return DrcRun(False, None, "kicad-cli not found; KiCad DRC skipped (internal checks only).")
    out = pcb.with_name("drc.json")
    cmd = [cli, "pcb", "drc", "--format", "json", "--severity-all", "-o", out.name, pcb.name]
    try:
        proc = subprocess.run(
            cmd, cwd=pcb.parent, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return DrcRun(False, None, f"kicad-cli failed to run: {e}")
    if not out.exists():
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        return DrcRun(False, None, f"kicad-cli produced no report (exit {proc.returncode}): {tail}")
    version = ""
    for line in proc.stdout.splitlines():
        if line.startswith("Found"):
            version += line + "; "
    return DrcRun(True, out, f"KiCad DRC ran via {Path(cli).name}: {version.strip('; ')}")
