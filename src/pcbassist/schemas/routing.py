"""Routing guidance."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import Model


class StackupLayer(Model):
    name: str
    kind: Literal["copper", "dielectric", "mask", "silk"]
    thickness_mm: float = Field(gt=0)
    purpose: str = ""


class Stackup(Model):
    layers: list[StackupLayer]
    total_thickness_mm: float
    copper_weight_oz: float = 1.0


class NetPriority(Model):
    order: int = Field(ge=1)
    target: str = Field(description="Net or net class")
    reason: str = ""


class DiffPair(Model):
    name: str
    positive: str
    negative: str
    impedance_ohms: float = 90.0
    notes: str = ""


class RoutingGuidance(Model):
    stackup: Stackup
    priorities: list[NetPriority] = Field(default_factory=list)
    trace_widths_mm: dict[str, float] = Field(default_factory=dict, description="Per net class")
    ground_strategy: str = ""
    diff_pairs: list[DiffPair] = Field(default_factory=list)
    length_matching: list[str] = Field(default_factory=list)
    return_path_notes: list[str] = Field(default_factory=list)
    via_guidance: list[str] = Field(default_factory=list)
    decoupling_notes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
