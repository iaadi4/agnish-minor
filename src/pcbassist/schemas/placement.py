"""Placement plan: zones, hints and rough coordinates."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import Model, Rect, Side

ZoneKind = Literal[
    "power", "rf", "mcu", "sensor", "connector", "analog", "driver", "debug", "general"
]


class Zone(Model):
    name: str
    kind: ZoneKind
    rect: Rect
    description: str = ""


class ProximityRule(Model):
    target_ref: str
    target_pin: str | None = None
    max_distance_mm: float = Field(gt=0)
    reason: str = ""

    def describe(self, ref: str) -> str:
        pin = f" pin {self.target_pin}" if self.target_pin else ""
        return f"{ref} within {self.max_distance_mm:g}mm of {self.target_ref}{pin}"


class PlacementHint(Model):
    ref: str
    zone: str
    side: Side = Side.TOP
    rotation_deg: float = 0.0
    near: list[ProximityRule] = Field(default_factory=list)
    edge: Literal["top", "bottom", "left", "right"] | None = None
    notes: str = ""


class ComponentPlacement(Model):
    ref: str
    x_mm: float = Field(description="Footprint origin X")
    y_mm: float = Field(description="Footprint origin Y")
    rotation_deg: float = 0.0
    side: Side = Side.TOP
    zone: str | None = None
    bbox: Rect | None = Field(default=None, description="Courtyard bounding box at this position")


class PlacementPlan(Model):
    zones: list[Zone] = Field(default_factory=list)
    hints: list[PlacementHint] = Field(default_factory=list)
    placements: list[ComponentPlacement] = Field(default_factory=list)
    algorithm: str = "zone shelf-packing with proximity ordering (deterministic)"
    notes: list[str] = Field(default_factory=list)
