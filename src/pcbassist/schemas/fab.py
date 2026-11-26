"""Fabrication/assembly (DFM) rule profiles loaded from ``data/fab_profiles/*.json``."""

from __future__ import annotations

from pydantic import Field

from .common import Model


class FabProfile(Model):
    name: str
    description: str
    layers: int = Field(ge=1, le=12)
    board_thickness_mm: float = 1.6
    copper_oz: float = 1.0
    min_trace_mm: float = Field(gt=0)
    min_space_mm: float = Field(gt=0)
    min_drill_mm: float = Field(gt=0)
    min_annular_ring_mm: float = Field(gt=0)
    min_via_diameter_mm: float = Field(gt=0)
    min_via_drill_mm: float = Field(gt=0)
    copper_to_edge_mm: float = Field(gt=0)
    component_to_edge_mm: float = Field(gt=0)
    courtyard_clearance_mm: float = Field(ge=0)
    min_pad_pitch_mm: float = Field(gt=0, description="Smallest pad pitch the assembler accepts")
    min_passive_size: str = Field(description="Smallest imperial passive size, e.g. '0402'")
    silk_to_pad_mm: float = Field(ge=0)
    min_silk_width_mm: float = Field(gt=0)
    allow_bga: bool = False
    allow_tht: bool = True
    notes: list[str] = Field(default_factory=list)
