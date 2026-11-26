from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pcbassist.paths import data_dir
from pcbassist.schemas import (
    DISCLAIMER,
    EXPORTED_MODELS,
    Architecture,
    CheckStatus,
    CheckSummary,
    Component,
    ComponentSelection,
    Constraints,
    DesignPlan,
    Finding,
    Netlist,
    Pin,
    PlacementPlan,
    Rect,
    Requirements,
    RoutingGuidance,
    SupplyRange,
    export_json_schemas,
)
from pcbassist.schemas.fab import FabProfile


def _finding(sev: str, i: int = 0) -> Finding:
    return Finding(id=f"X:{i}", rule="X", severity=sev, category="ERC", message="m")


def test_extra_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Pin.model_validate({"number": "1", "name": "VDD", "type": "power_in", "voltage": 3.3})


def test_pin_type_enum_enforced() -> None:
    with pytest.raises(ValidationError):
        Pin.model_validate({"number": "1", "name": "X", "type": "power"})


@pytest.mark.parametrize("ref", ["U1", "C12", "FB2", "BT1", "SW10"])
def test_refdes_valid(ref: str) -> None:
    Component(ref_des=ref, role="r", mpn="m")


@pytest.mark.parametrize("ref", ["u1", "1U", "U", "U1A", "TOOLONG1"])
def test_refdes_invalid(ref: str) -> None:
    with pytest.raises(ValidationError):
        Component(ref_des=ref, role="r", mpn="m")


def test_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        Component(ref_des="U1", role="r", mpn="m", confidence=1.5)


def test_supply_range_order_and_contains() -> None:
    s = SupplyRange(min_v=3.0, max_v=3.6)
    assert s.contains(3.3) and not s.contains(5.0)
    with pytest.raises(ValidationError, match="min_v"):
        SupplyRange(min_v=5, max_v=3)


def test_component_pin_lookup() -> None:
    c = Component(
        ref_des="U1",
        role="ldo",
        mpn="X",
        pins=[
            Pin(number="1", name="GND", type="power_in"),
            Pin(number="2", name="GND", type="passive"),
        ],
    )
    assert c.pin("2") is not None and c.pin("9") is None
    assert len(c.pins_named("GND")) == 2


def test_architecture_validation_and_mermaid() -> None:
    a = Architecture.model_validate(
        {
            "blocks": [
                {"id": "mcu", "kind": "mcu_wireless", "name": "ESP32 (WiFi)"},
                {"id": "ldo", "kind": "regulation", "name": "LDO"},
            ],
            "links": [
                {"src": "ldo", "dst": "mcu", "kind": "power", "label": "+3V3"},
                {"src": "mcu", "dst": "ldo", "kind": "gpio", "label": "EN"},
            ],
        }
    )
    m = a.to_mermaid()
    assert m.startswith("flowchart LR")
    assert "ldo ==>|+3V3| mcu" in m and "mcu -->|EN| ldo" in m
    assert "(" not in m.split("\n")[1]  # special chars stripped from labels
    assert a.block("mcu") is not None and a.block("nope") is None
    with pytest.raises(ValidationError, match="unknown blocks"):
        Architecture.model_validate(
            {
                "blocks": [{"id": "a", "kind": "mcu", "name": "A"}],
                "links": [{"src": "a", "dst": "b", "kind": "power"}],
            }
        )
    with pytest.raises(ValidationError, match="duplicate block ids"):
        Architecture.model_validate(
            {
                "blocks": [
                    {"id": "a", "kind": "mcu", "name": "A"},
                    {"id": "a", "kind": "mcu", "name": "B"},
                ]
            }
        )
    with pytest.raises(ValidationError):
        Architecture.model_validate({"blocks": [{"id": "Bad-Id", "kind": "mcu", "name": "A"}]})


def test_netlist_duplicate_names_and_lookup() -> None:
    nl = Netlist.model_validate(
        {
            "nets": [
                {"name": "+3V3", "net_class": "power", "connections": [{"ref": "U1", "pin": "1"}]},
                {"name": "GND", "net_class": "ground", "connections": [{"ref": "U1", "pin": "2"}]},
            ]
        }
    )
    assert nl.net("GND") is not None
    assert [n.name for n in nl.nets_of("U1", "1")] == ["+3V3"]
    with pytest.raises(ValidationError, match="duplicate net names"):
        Netlist.model_validate({"nets": [{"name": "A"}, {"name": "A"}]})
    with pytest.raises(ValidationError):
        Netlist.model_validate({"nets": [{"name": "bad name"}]})
    with pytest.raises(ValidationError):
        Netlist.model_validate({"no_connects": [{"ref": "U1", "pin": "4", "reason": ""}]})


def test_rect_geometry() -> None:
    a = Rect(x_mm=0, y_mm=0, w_mm=10, h_mm=10)
    assert a.intersects(Rect(x_mm=5, y_mm=5, w_mm=10, h_mm=10))
    assert not a.intersects(Rect(x_mm=10, y_mm=0, w_mm=5, h_mm=5))  # touching edges is OK
    assert a.contains(Rect(x_mm=1, y_mm=1, w_mm=2, h_mm=2))
    assert not a.contains(Rect(x_mm=9, y_mm=9, w_mm=2, h_mm=2))
    with pytest.raises(ValidationError):
        Rect(x_mm=0, y_mm=0, w_mm=0, h_mm=1)


@pytest.mark.parametrize(
    "sevs,status",
    [
        ([], CheckStatus.PASS),
        (["info"], CheckStatus.PASS),
        (["info", "warning"], CheckStatus.PASS_WITH_WARNINGS),
        (["warning", "error"], CheckStatus.FAIL),
    ],
)
def test_check_summary_status(sevs: list[str], status: CheckStatus) -> None:
    s = CheckSummary.from_findings([_finding(v, i) for i, v in enumerate(sevs)])
    assert s.status == status
    assert s.errors + s.warnings + s.infos == len(sevs)


def test_requirements_defaults_and_literals() -> None:
    r = Requirements(summary="x")
    assert r.assumptions == [] and r.open_questions == [] and r.environment.temp_max_c == 70
    with pytest.raises(ValidationError):
        Requirements.model_validate(
            {
                "summary": "x",
                "power_sources": [{"kind": "nuclear", "nominal_v": 1, "min_v": 1, "max_v": 1}],
            }
        )


def _full_plan() -> DesignPlan:
    return DesignPlan.model_validate(
        {
            "prompt": "test board",
            "requirements": {"summary": "s"},
            "architecture": {"blocks": [{"id": "mcu", "kind": "mcu", "name": "MCU"}]},
            "components": [{"ref_des": "U1", "role": "mcu", "mpn": "X", "verified": True}],
            "netlist": {
                "nets": [
                    {
                        "name": "GND",
                        "net_class": "ground",
                        "connections": [{"ref": "U1", "pin": "1"}],
                    }
                ]
            },
            "constraints": Constraints.model_validate(
                {"electrical": {}, "mechanical": {"outline": {"width_mm": 50, "height_mm": 30}}}
            ).model_dump(),
            "placement": PlacementPlan().model_dump(),
            "routing": RoutingGuidance.model_validate(
                {
                    "stackup": {
                        "layers": [{"name": "F.Cu", "kind": "copper", "thickness_mm": 0.035}],
                        "total_thickness_mm": 1.6,
                    }
                }
            ).model_dump(),
            "findings": [_finding("warning").model_dump()],
        }
    )


def test_design_plan_round_trip_and_disclaimer() -> None:
    plan = _full_plan()
    assert plan.disclaimer == DISCLAIMER and "qualified" in DISCLAIMER
    again = DesignPlan.model_validate_json(plan.model_dump_json())
    assert again == plan
    assert plan.component("U1") is not None and plan.component("U9") is None


def test_component_selection_schema() -> None:
    sel = ComponentSelection.model_validate(
        {
            "parts": [
                {
                    "role": "mcu",
                    "block_id": "mcu",
                    "mpn": "ESP32-C3-WROOM-02-N4",
                    "rationale": "r",
                    "confidence": 0.9,
                }
            ]
        }
    )
    assert sel.parts[0].quantity == 1


def test_json_schema_export(tmp_path: Path) -> None:
    written = export_json_schemas(tmp_path)
    assert len(written) == len(EXPORTED_MODELS)
    for p in written:
        schema = json.loads(p.read_text())
        assert schema.get("type") == "object" or "$defs" in schema or "properties" in schema
    plan_schema = json.loads((tmp_path / "DesignPlan.schema.json").read_text())
    assert "requirements" in plan_schema["properties"]
    assert plan_schema.get("additionalProperties") is False


@pytest.mark.parametrize("name", ["generic_2layer", "generic_4layer"])
def test_fab_profiles_validate(name: str) -> None:
    prof = FabProfile.model_validate_json(
        (data_dir() / "fab_profiles" / f"{name}.json").read_text()
    )
    assert prof.name == name
    assert prof.min_via_drill_mm < prof.min_via_diameter_mm
