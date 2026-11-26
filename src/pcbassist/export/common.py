"""Shared helpers for exporters: deterministic UUIDs, names, disclaimer text."""

from __future__ import annotations

import re
import uuid

from .. import __version__
from ..schemas import DISCLAIMER, DesignPlan

_NS = uuid.UUID("6b1f0c8e-6a52-4c3e-9d53-0c4a2f0e7a11")

#: Board origin on the KiCad page (mm), so the board is not drawn at the page corner.
BOARD_ORIGIN = (100.0, 50.0)

SHORT_DISCLAIMER = "AI-GENERATED - NOT VERIFIED FOR FABRICATION - REVIEW BY A QUALIFIED ENGINEER"


def det_uuid(*parts: object) -> str:
    """Stable UUID so re-exporting the same plan gives byte-identical files."""
    return str(uuid.uuid5(_NS, "/".join(str(p) for p in parts)))


def project_name(plan: DesignPlan) -> str:
    base = plan.requirements.summary if plan.requirements else plan.prompt
    words = re.findall(r"[A-Za-z0-9]+", base.lower())[:5]
    return "_".join(words) or "pcbassist_design"


def tool_string() -> str:
    return f"pcbassist {__version__}"


__all__ = ["BOARD_ORIGIN", "DISCLAIMER", "SHORT_DISCLAIMER", "det_uuid", "project_name"]
