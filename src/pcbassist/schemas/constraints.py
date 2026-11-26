"""Electrical and mechanical constraints."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import Edge, ItemStatus, Model, NetClassName, Rect


class RailConstraint(Model):
    name: str
    nominal_v: float
    min_v: float
    max_v: float
    max_current_ma: float | None = Field(default=None, description="Source capability")
    budget_ma: float | None = Field(default=None, description="Sum of worst-case loads")
    source_ref: str | None = Field(
        default=None, description="Ref of the regulator/source driving it"
    )
    source_kind: Literal[
        "usb", "dc_input", "battery", "ldo", "buck", "boost", "charger", "load_switch", "other"
    ] = "other"
    parent_rail: str | None = Field(default=None, description="Upstream rail feeding the source")


class NetClassRule(Model):
    name: str
    kind: NetClassName
    trace_width_mm: float = Field(gt=0)
    clearance_mm: float = Field(gt=0)
    via_diameter_mm: float = Field(gt=0)
    via_drill_mm: float = Field(gt=0)
    nets: list[str] = Field(default_factory=list)
    max_current_ma: float | None = None
    rationale: str = ""


class DecouplingRequirement(Model):
    ref: str
    pin: str
    capacitors: list[str] = Field(description="Refs of the capacitors serving this pin")
    max_distance_mm: float = 3.0


class ImpedanceTarget(Model):
    target: str = Field(description="Net or net-class name")
    ohms: float
    kind: Literal["single_ended", "differential"] = "single_ended"
    notes: str = ""


class Keepout(Model):
    name: str
    kind: Literal["rf", "copper", "component", "via", "mechanical"]
    rect: Rect
    layers: list[str] = Field(default_factory=lambda: ["F.Cu", "B.Cu"])
    reason: str = ""


class ElectricalConstraints(Model):
    rails: list[RailConstraint] = Field(default_factory=list)
    net_classes: list[NetClassRule] = Field(default_factory=list)
    decoupling: list[DecouplingRequirement] = Field(default_factory=list)
    impedance: list[ImpedanceTarget] = Field(default_factory=list)
    rf_keepouts: list[Keepout] = Field(default_factory=list)
    trace_width_method: str = Field(
        default=(
            "IPC-2221 external layer, dT=10C, 1 oz Cu: I = 0.048 * dT^0.44 * A^0.725 (A in mil^2)"
        )
    )
    notes: list[str] = Field(default_factory=list)


class BoardOutline(Model):
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    corner_radius_mm: float = Field(default=1.0, ge=0)
    thickness_mm: float = Field(default=1.6, gt=0)


class MountingHole(Model):
    ref: str
    x_mm: float
    y_mm: float
    drill_mm: float = 3.2
    pad_mm: float | None = None
    plated: bool = False


class ConnectorPlacement(Model):
    ref: str
    edge: Edge
    offset_mm: float | None = Field(
        default=None, description="Distance along the edge from its start"
    )
    reason: str = ""


class MechanicalConstraints(Model):
    outline: BoardOutline
    layer_count: int = Field(default=2, ge=1, le=12)
    mounting_holes: list[MountingHole] = Field(default_factory=list)
    connectors: list[ConnectorPlacement] = Field(default_factory=list)
    keepouts: list[Keepout] = Field(default_factory=list)
    max_component_height_mm: float | None = None
    fab_profile: str = "generic_2layer"


class Constraints(Model):
    electrical: ElectricalConstraints
    mechanical: MechanicalConstraints
    status: ItemStatus = ItemStatus.PROPOSED
    locked: bool = False
