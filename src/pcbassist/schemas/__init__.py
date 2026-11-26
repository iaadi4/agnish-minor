"""Pydantic v2 data models for every stage of the design pipeline."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from .architecture import Architecture, Block, BlockLink
from .common import (
    DISCLAIMER,
    SCHEMA_VERSION,
    Edge,
    FindingCategory,
    FindingSource,
    ItemStatus,
    NetClassName,
    PinType,
    Rect,
    ReviewStatus,
    Severity,
    Side,
)
from .component import Component, CurrentProfile, FootprintInfo, LibraryPart, Pin, SupplyRange
from .constraints import (
    BoardOutline,
    ConnectorPlacement,
    Constraints,
    DecouplingRequirement,
    ElectricalConstraints,
    ImpedanceTarget,
    Keepout,
    MechanicalConstraints,
    MountingHole,
    NetClassRule,
    RailConstraint,
)
from .fab import FabProfile
from .findings import CheckStatus, CheckSummary, Finding
from .llm_outputs import ComponentSelection, SelectedPart
from .netlist import Net, NetConnection, Netlist, NoConnect
from .placement import ComponentPlacement, PlacementHint, PlacementPlan, ProximityRule, Zone
from .plan import DesignPlan, LLMProvenance
from .requirements import Assumption, Requirements
from .review import AuditEntry, ItemRef, ReviewComment, ReviewDecision
from .routing import DiffPair, NetPriority, RoutingGuidance, Stackup, StackupLayer

#: Every model that gets a published JSON Schema.
EXPORTED_MODELS: dict[str, type[BaseModel]] = {
    "Requirements": Requirements,
    "Architecture": Architecture,
    "Pin": Pin,
    "Component": Component,
    "LibraryPart": LibraryPart,
    "ComponentSelection": ComponentSelection,
    "Net": Net,
    "Netlist": Netlist,
    "Constraints": Constraints,
    "ElectricalConstraints": ElectricalConstraints,
    "MechanicalConstraints": MechanicalConstraints,
    "PlacementPlan": PlacementPlan,
    "RoutingGuidance": RoutingGuidance,
    "Finding": Finding,
    "ReviewComment": ReviewComment,
    "ReviewDecision": ReviewDecision,
    "AuditEntry": AuditEntry,
    "DesignPlan": DesignPlan,
}


def json_schema(model: type[BaseModel]) -> dict[str, object]:
    return model.model_json_schema(mode="validation")


def export_json_schemas(out_dir: Path) -> list[Path]:
    """Write one ``<Model>.schema.json`` per exported model; returns the written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in EXPORTED_MODELS.items():
        path = out_dir / f"{name}.schema.json"
        path.write_text(json.dumps(json_schema(model), indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


__all__ = [
    "DISCLAIMER",
    "EXPORTED_MODELS",
    "SCHEMA_VERSION",
    "Architecture",
    "Assumption",
    "AuditEntry",
    "Block",
    "BlockLink",
    "BoardOutline",
    "CheckStatus",
    "CheckSummary",
    "Component",
    "ComponentPlacement",
    "ComponentSelection",
    "ConnectorPlacement",
    "Constraints",
    "CurrentProfile",
    "DecouplingRequirement",
    "DesignPlan",
    "DiffPair",
    "Edge",
    "ElectricalConstraints",
    "FabProfile",
    "Finding",
    "FindingCategory",
    "FindingSource",
    "FootprintInfo",
    "ImpedanceTarget",
    "ItemRef",
    "ItemStatus",
    "Keepout",
    "LLMProvenance",
    "LibraryPart",
    "MechanicalConstraints",
    "MountingHole",
    "Net",
    "NetClassName",
    "NetClassRule",
    "NetConnection",
    "NetPriority",
    "Netlist",
    "NoConnect",
    "Pin",
    "PinType",
    "PlacementHint",
    "PlacementPlan",
    "ProximityRule",
    "RailConstraint",
    "Rect",
    "Requirements",
    "ReviewComment",
    "ReviewDecision",
    "ReviewStatus",
    "RoutingGuidance",
    "SelectedPart",
    "Severity",
    "Side",
    "Stackup",
    "StackupLayer",
    "SupplyRange",
    "Zone",
    "export_json_schemas",
    "json_schema",
]
