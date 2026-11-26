"""KiCad 8 project file (``.kicad_pro``): design rules and net classes from the plan."""

from __future__ import annotations

import json
from typing import Any

from ..fab import load_fab_profile
from ..schemas import DISCLAIMER, DesignPlan
from .common import det_uuid


def _netclass(
    name: str, width: float, clearance: float, via_d: float, via_drill: float
) -> dict[str, Any]:
    return {
        "name": name,
        "clearance": clearance,
        "track_width": width,
        "via_diameter": via_d,
        "via_drill": via_drill,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "diff_pair_width": max(width, 0.2),
        "diff_pair_gap": max(clearance, 0.2),
        "diff_pair_via_gap": 0.25,
        "wire_width": 6,
        "bus_width": 12,
        "line_style": 0,
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "pcb_color": "rgba(0, 0, 0, 0.000)",
    }


def build_project(plan: DesignPlan, name: str = "board") -> dict[str, Any]:
    assert plan.constraints is not None
    fab = load_fab_profile(plan.constraints.mechanical.fab_profile)
    classes = []
    patterns = []
    for nc in plan.constraints.electrical.net_classes:
        classes.append(
            _netclass(
                nc.name, nc.trace_width_mm, nc.clearance_mm, nc.via_diameter_mm, nc.via_drill_mm
            )
        )
        if nc.name != "Default":
            patterns += [{"netclass": nc.name, "pattern": net} for net in nc.nets]
    if not any(c["name"] == "Default" for c in classes):
        classes.insert(0, _netclass("Default", 0.2, 0.2, 0.6, 0.3))
    rules = {
        "min_clearance": fab.min_space_mm,
        "min_track_width": fab.min_trace_mm,
        "min_via_diameter": fab.min_via_diameter_mm,
        "min_via_annular_width": fab.min_annular_ring_mm,
        "min_through_hole_diameter": fab.min_drill_mm,
        "min_hole_clearance": 0.25,
        "min_hole_to_hole": 0.25,
        "min_copper_edge_clearance": fab.copper_to_edge_mm,
        "min_microvia_diameter": 0.2,
        "min_microvia_drill": 0.1,
        "min_silk_clearance": 0.0,
        "min_text_height": 0.8,
        "min_text_thickness": 0.08,
        "solder_mask_to_copper_clearance": 0.0,
        "max_error": 0.005,
        "allow_blind_buried_vias": False,
        "allow_microvias": False,
    }
    return {
        "board": {
            "design_settings": {
                "defaults": {"board_outline_line_width": 0.1, "copper_line_width": 0.2},
                "rules": rules,
                "track_widths": [
                    0.0,
                    *sorted({nc.trace_width_mm for nc in plan.constraints.electrical.net_classes}),
                ],
                "via_dimensions": [{"diameter": 0.0, "drill": 0.0}],
                "diff_pair_dimensions": [{"gap": 0.0, "via_gap": 0.0, "width": 0.0}],
            },
            "layer_presets": [],
            "viewports": [],
        },
        "boards": [],
        "meta": {"filename": f"{name}.kicad_pro", "version": 1},
        "net_settings": {
            "classes": classes,
            "meta": {"version": 3},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": patterns,
        },
        "pcbnew": {"page_layout_descr_file": ""},
        "sheets": [[det_uuid("schematic-root"), "Root"]],
        "text_variables": {"DISCLAIMER": DISCLAIMER, "FAB_PROFILE": fab.name},
    }


def write_project(plan: DesignPlan, name: str = "board") -> str:
    return json.dumps(build_project(plan, name), indent=2) + "\n"
