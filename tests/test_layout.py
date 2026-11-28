from __future__ import annotations

import pytest

from conftest import EXAMPLES
from pcbassist.kicad.footprint import (
    load_footprint,
    pad_position,
    parse_footprint,
    placed_bbox,
    placed_rects,
    rectilinear_rects,
    rotate,
)
from pcbassist.pipeline.constraints import choose_fab_profile, ipc2221_width_mm
from pcbassist.pipeline.routing import stackup_for
from pcbassist.schemas import DesignPlan, Edge

GOOD = ["iot_env_sensor", "ble_coin_tag", "relay_motor_12v"]


def _ov(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def test_ipc2221_reference_values() -> None:
    assert ipc2221_width_mm(1.0) == pytest.approx(0.300, abs=0.005)
    assert ipc2221_width_mm(2.0) > ipc2221_width_mm(1.0)
    assert ipc2221_width_mm(1.0, external=False) > ipc2221_width_mm(1.0)
    assert ipc2221_width_mm(1.0, copper_oz=2.0) == pytest.approx(0.150, abs=0.003)
    assert ipc2221_width_mm(0) == 0.0


@pytest.mark.parametrize("name", EXAMPLES)
def test_constraints_present(plans: dict[str, DesignPlan], name: str) -> None:
    plan = plans[name]
    cons = plan.constraints
    assert cons is not None
    assert {r.name for r in cons.electrical.rails} == {r.name for r in plan.power_tree}
    assert all(r.budget_ma is not None for r in cons.electrical.rails)
    classes = {nc.name: nc for nc in cons.electrical.net_classes}
    assert "Default" in classes and classes["Default"].trace_width_mm >= 0.15
    power = classes["Power"]
    assert power.trace_width_mm >= ipc2221_width_mm((power.max_current_ma or 0) / 1000)
    assert power.trace_width_mm >= 0.4
    req = plan.requirements
    assert req is not None
    if req.size.max_width_mm:
        assert cons.mechanical.outline.width_mm == req.size.max_width_mm
    assert cons.mechanical.fab_profile == "generic_2layer"


def test_iot_constraints_details(plans: dict[str, DesignPlan]) -> None:
    cons = plans["iot_env_sensor"].constraints
    assert cons is not None
    assert any(i.target == "USB" and i.ohms == 90 for i in cons.electrical.impedance)
    edges = {cp.ref: cp.edge for cp in cons.mechanical.connectors}
    assert Edge.LEFT in edges.values() and Edge.TOP in edges.values()
    assert cons.electrical.decoupling
    assert cons.electrical.rf_keepouts and cons.electrical.rf_keepouts[0].kind == "rf"


def test_mounting_holes_at_corners(plans: dict[str, DesignPlan]) -> None:
    cons = plans["relay_motor_12v"].constraints
    assert cons is not None
    xy = sorted((h.x_mm, h.y_mm) for h in cons.mechanical.mounting_holes)
    assert xy == [(4.0, 4.0), (4.0, 56.0), (76.0, 4.0), (76.0, 56.0)]


@pytest.mark.parametrize("name", GOOD)
def test_placement_is_legal(plans: dict[str, DesignPlan], name: str) -> None:
    plan = plans[name]
    assert plan.placement is not None and plan.constraints is not None
    w = plan.constraints.mechanical.outline.width_mm
    h = plan.constraints.mechanical.outline.height_mm
    placed = {p.ref: p for p in plan.placement.placements}
    comps = {c.ref_des: c for c in plan.components}
    for c in plan.components:
        if c.pins_known and c.footprint:
            assert c.ref_des in placed, f"{c.ref_des} unplaced"
    shapes = {}
    for ref, p in placed.items():
        g = load_footprint(comps[ref].footprint or "")
        assert g is not None
        shapes[ref] = placed_rects(g, p.x_mm, p.y_mm, p.rotation_deg)
        for r in shapes[ref]:
            assert r[0] >= -1e-6 and r[1] >= -1e-6 and r[2] <= w + 1e-6 and r[3] <= h + 1e-6
    refs = sorted(shapes)
    for i, a in enumerate(refs):
        for b in refs[i + 1 :]:
            assert not any(_ov(x, y) for x in shapes[a] for y in shapes[b]), f"{a} overlaps {b}"
    for ko in plan.constraints.electrical.rf_keepouts:
        owner = ko.name.split("_")[0]
        box = (ko.rect.x_mm, ko.rect.y_mm, ko.rect.x2, ko.rect.y2)
        for ref, rs in shapes.items():
            if ref != owner:
                assert not any(_ov(r, box) for r in rs), f"{ref} in antenna keepout"
    assert not any(f.rule.startswith("PLACE-") for f in plan.findings)
    zones = {z.name for z in plan.placement.zones}
    assert "mcu" in zones
    assert plan.placement.hints


def test_placement_is_deterministic(plans: dict[str, DesignPlan], plan_copy) -> None:  # type: ignore[no-untyped-def]
    from pcbassist.pipeline.placement import place

    p = plan_copy("iot_env_sensor")
    again, _, _ = place(p)
    assert again.placements == plans["iot_env_sensor"].placement.placements  # type: ignore[union-attr]


def test_decoupling_caps_are_near_the_mcu(plans: dict[str, DesignPlan]) -> None:
    plan = plans["iot_env_sensor"]
    assert not [f for f in plan.findings if f.rule == "DFM-DECOUPLING-DISTANCE" and "U3" in f.refs]


def test_flawed_board_is_too_small(plans: dict[str, DesignPlan]) -> None:
    rules = {f.rule for f in plans["flawed_coin_wifi"].findings}
    assert {"CON-BOARD-AREA", "PLACE-NO-SPACE", "DFM-UNPLACED"} <= rules


def test_routing_guidance(plans: dict[str, DesignPlan]) -> None:
    iot = plans["iot_env_sensor"].routing
    assert iot is not None
    assert [layer.name for layer in iot.stackup.layers] == ["F.Cu", "Core", "B.Cu"]
    assert iot.priorities[0].target == "antenna keepout"
    assert iot.diff_pairs and iot.diff_pairs[0].impedance_ohms == 90
    assert "B.Cu" in iot.ground_strategy
    relay = plans["relay_motor_12v"].routing
    assert relay is not None
    assert any("SW_5V" in p.target for p in relay.priorities)
    assert any("Motor" in n for n in relay.return_path_notes)
    four = stackup_for(4)
    assert len([la for la in four.layers if la.kind == "copper"]) == 4
    assert four.total_thickness_mm == 1.6


def test_fab_profile_choice(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    assert choose_fab_profile(p) == "generic_2layer"
    p.requirements.layer_count_target = 4
    assert choose_fab_profile(p) == "generic_4layer"
    assert choose_fab_profile(p, "x") == "x"


def test_footprint_geometry_helpers() -> None:
    g = load_footprint("RF_Module:ESP32-C3-WROOM-02")
    assert g is not None and g.has_courtyard and len(g.rects) == 2
    assert g.smd  # thermal vias inside the GND pad do not make it THT
    assert load_footprint("Nope:Nothing") is None
    assert rotate(1, 0, 90) == pytest.approx((0, -1))
    assert placed_bbox(g, 10, 10, 0)[0] == pytest.approx(10 + g.courtyard[0])
    assert pad_position(g, "1", 0, 0) is not None
    assert pad_position(g, "999", 0, 0) is None
    rot = placed_bbox(g, 0, 0, 90)
    assert rot[2] - rot[0] == pytest.approx(g.height)
    # L-shape decomposes into two rectangles; a diagonal edge does not decompose
    L = [(0, 0, 2, 0), (2, 0, 2, 1), (2, 1, 1, 1), (1, 1, 1, 2), (1, 2, 0, 2), (0, 2, 0, 0)]
    assert sorted(rectilinear_rects(L) or []) == [(0, 0, 2, 1), (0, 1, 1, 2)]
    assert rectilinear_rects([(0, 0, 1, 1)]) is None
    text = '(footprint "X" (layer "F.Cu") (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))'
    bare = parse_footprint(text, "L:X", g.path)
    assert not bare.has_courtyard and bare.width == pytest.approx(1.5)
