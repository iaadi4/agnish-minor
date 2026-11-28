"""The intent engine: KiCad index, manifest/netlist validators, the tool loop, repair rounds."""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import example_prompt
from pcbassist.kicad.footprint import load_footprint, synthesize_footprint
from pcbassist.kicad.fpindex import default_footprint_index
from pcbassist.kicad.sexpr import dumps, parse
from pcbassist.kicad.symbols import default_symbol_index
from pcbassist.library import ComponentLibrary
from pcbassist.llm import AuditLog, MockLLMClient
from pcbassist.llm.agent import Tool, run_validated_stage
from pcbassist.llm.base import ToolCall, ToolRequest, ToolSpec, ToolTurn
from pcbassist.pipeline import PipelineOptions, generate_plan
from pcbassist.pipeline.design import Libraries, lookup_tools, pin_dossier, repair_problems
from pcbassist.pipeline.intent import (
    build_netlist,
    infer_category,
    looks_like_description,
    passive_default,
    resolve_manifest,
    validate_intent,
)
from pcbassist.schemas import Architecture, Block, DesignPlan
from pcbassist.schemas.intent import NetlistIntent, PartsManifest

SYM = default_symbol_index()
FPS = default_footprint_index()


def _arch() -> Architecture:
    return Architecture(
        blocks=[
            Block(id="mon", kind="sensor", name="monitor"),
            Block(id="pwr", kind="regulation", name="power"),
        ]
    )


def _manifest(**extra: Any) -> PartsManifest:
    parts: list[dict[str, Any]] = [
        {
            "ref": "U1",
            "value": "INA226",
            "block_id": "mon",
            "role": "current monitor",
            "mpn": "INA226AIDGSR",
            "lib_id": "Sensor_Energy:INA226",
            "footprint": "Package_SO:VSSOP-10_3x3mm_P0.5mm",
            "ratings": {"supply_min_v": 2.7, "supply_max_v": 5.5, "typ_ma": 0.33},
        },
        {
            "ref": "C1",
            "value": "100nF 16V",
            "block_id": "mon",
            "role": "dec",
            "package": "0402",
            "support_for": "U1",
        },
        {"ref": "R1", "value": "4.7k", "block_id": "mon", "role": "SDA pull-up", "package": "0402"},
        {"ref": "R2", "value": "4.7k", "block_id": "mon", "role": "SCL pull-up", "package": "0402"},
        {
            "ref": "J1",
            "value": "Conn_01x04",
            "block_id": "pwr",
            "role": "I2C + power in",
            "mpn": "61300411121",
        },
    ]
    return PartsManifest.model_validate({"parts": parts, **extra})


def _resolve(m: PartsManifest, lib: ComponentLibrary) -> Any:
    return resolve_manifest(m, _arch(), lib, SYM, FPS)


def _intent() -> NetlistIntent:
    return NetlistIntent.model_validate(
        {
            "nets": [
                {
                    "name": "+3V3",
                    "kind": "power",
                    "voltage_v": 3.3,
                    "pins": ["J1.1", "U1.6", "C1.1", "R1.1", "R2.1"],
                },
                {"name": "GND", "kind": "ground", "pins": ["J1.2", "U1.7", "C1.2", "U1.1", "U1.2"]},
                {"name": "I2C_SDA", "pins": ["J1.3", "U1.4", "R1.2"]},
                {"name": "I2C_SCL", "pins": ["J1.4", "U1.5", "R2.2"]},
                {"name": "VSENSE", "pins": ["U1.8", "U1.9"]},
            ],
            "no_connect": [
                {"pin": "U1.3", "reason": "alert unused"},
                {"pin": "U1.10", "reason": "demo only"},
            ],
            "rails": [
                {
                    "net": "+3V3",
                    "voltage_v": 3.3,
                    "source_ref": "J1",
                    "source_kind": "other",
                    "max_current_ma": 100,
                }
            ],
        }
    )


# -- KiCad index ----------------------------------------------------------------------


def test_symbol_index_finds_parts_by_mpn() -> None:
    assert len(SYM) > 20000 and len(FPS) > 14000
    assert SYM.search("STM32F103C8T6")[0].lib_id == "MCU_ST_STM32F1:STM32F103C8Tx"  # x wildcard
    assert SYM.search("INA226")[0].lib_id == "Sensor_Energy:INA226"
    assert SYM.search("AP2112K-3.3")[0].lib_id == "Regulator_Linear:AP2112K-3.3"
    ina = SYM.get("Sensor_Energy:INA226")
    assert ina is not None and ina.pin("6") is not None and ina.pin("6").type == "power_in"
    assert ina.datasheet and ina.datasheet.startswith("http")


def test_symbol_resolution_suggests_the_real_lib_id() -> None:
    r = SYM.resolve("Sensor:INA226")
    assert r.info is None and r.why == "found-elsewhere" and "Sensor_Energy:INA226" in r.candidates
    r = SYM.resolve("Sensor_Energy:INA2266")
    assert r.why == "no-symbol" and "Sensor_Energy:INA226" in r.candidates


def test_footprint_index_geometry_and_rebuild() -> None:
    name = "Package_SO:VSSOP-10_3x3mm_P0.5mm"
    assert FPS.pad_numbers(name) == {str(i) for i in range(1, 11)}
    g = load_footprint(name)  # not vendored: comes from the index in hermetic tests
    assert g is not None and g.smd and len(g.pads) == 10 and g.width > 4
    fp = synthesize_footprint(g)
    back = parse(dumps(fp))
    assert sum(1 for x in back if isinstance(x, list) and x and x[0] == "pad") == 10
    assert FPS.nearest("Package_SO:VSSOP-10_3x3mm_P0.5")  # fuzzy suggestion


def test_lookup_tools_answer_from_the_index(library: ComponentLibrary) -> None:
    tools = {t.spec.name: t for t in lookup_tools(Libraries(library))}
    assert "Sensor_Energy:INA226" in tools["search_symbols"].run({"query": "INA226"})
    pins = tools["symbol_pins"].run({"lib_id": "Sensor_Energy:INA226"})
    assert "6=VS/power_in" in pins and "VSSOP-10" in pins
    assert "installed as" in tools["symbol_pins"].run({"lib_id": "Sensor:INA226"})
    fp = tools["check_footprints"].run({"footprints": ["Resistor_SMD:R_0402_1005Metric", "X:Y"]})
    assert fp.startswith("OK") and "MISS X:Y" in fp
    assert "AP2112K" in tools["search_parts"].run({"query": "ldo 3.3"})


# -- manifest validation --------------------------------------------------------------


def test_manifest_resolves_curated_kicad_and_passive_parts(library: ComponentLibrary) -> None:
    r = _resolve(_manifest(), library)
    assert r.ok, r.errors
    by = {c.ref_des: c for c in r.components}
    assert by["U1"].data_source == "kicad" and by["U1"].pins_verified and not by["U1"].verified
    assert by["U1"].supply_range is not None and by["U1"].category == "sensor_env"
    assert by["C1"].footprint == "Capacitor_SMD:C_0402_1005Metric"
    assert by["C1"].key_specs["voltage_v"] == 16.0 and by["C1"].category == "capacitor"
    assert by["J1"].verified and by["J1"].data_source == "library"
    rules = {f.rule for f in r.findings}
    assert {"LIB-KICAD-ONLY", "LIB-CLAIMED-RATINGS"} <= rules


@pytest.mark.parametrize(
    ("patch", "expect"),
    [
        ({"lib_id": "Sensor:INA226"}, "installed as: Sensor_Energy:INA226"),
        ({"footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"}, "are not pads of"),
        ({"footprint": "Package_SO:VSSOP-10_3x3mm"}, "not in the KiCad libraries"),
        ({"block_id": "nope"}, "is not an architecture block"),
        ({"value": "current monitor chip, for the motor"}, "reads as a description"),
        ({"mpn": None}, 'no "mpn"'),
        ({"lib_id": "power:GND"}, "power symbol"),
    ],
)
def test_manifest_errors_name_the_fix(
    library: ComponentLibrary, patch: dict[str, Any], expect: str
) -> None:
    m = _manifest()
    m.parts[0] = m.parts[0].model_copy(update=patch)
    r = _resolve(m, library)
    assert any(expect in e for e in r.errors), r.errors


def test_manifest_curated_mismatch_and_duplicates(library: ComponentLibrary) -> None:
    m = _manifest()
    m.parts[4] = m.parts[4].model_copy(update={"footprint": "Connector:Foo"})
    m.parts.append(m.parts[1].model_copy())
    r = _resolve(m, library)
    assert any("is a curated part with footprint" in e for e in r.errors)
    assert any("duplicate refdes C1" in e for e in r.errors)


def test_blockers_become_errors(library: ComponentLibrary) -> None:
    m = _manifest(
        blockers=[
            {
                "constraint": "2 years on CR2032",
                "required": "<12 uA avg",
                "found": "ESP32-C3 averages 350 uA at 10 s uploads",
            }
        ]
    )
    r = _resolve(m, library)
    assert any(f.rule == "SEL-BLOCKER" and f.severity.value == "error" for f in r.findings)


def test_passive_table_and_description_heuristic() -> None:
    assert passive_default("R", "0603") == ("Device:R", "Resistor_SMD:R_0603_1608Metric")
    assert passive_default("D", "SOD123") == ("Device:D", "Diode_SMD:D_SOD-123")
    assert passive_default("R", "0201") is None
    assert looks_like_description("P-MOSFET, divider gate")
    assert not looks_like_description("4.7uF, X5R, 10V, 0603")
    assert not looks_like_description("STM32F103C8T6")
    assert infer_category(SYM.get("Regulator_Switching:TPS62160DGK"), "U2") == "buck"  # type: ignore[arg-type]


# -- netlist intent validation ---------------------------------------------------------


def test_intent_validates_and_builds(library: ComponentLibrary) -> None:
    comps = _resolve(_manifest(), library).components
    r = validate_intent(_intent(), comps)
    assert r.ok, r.errors
    netlist, rails, annotated = build_netlist(_intent(), comps)
    by = {c.ref_des: c for c in annotated}
    assert by["U1"].rail == "+3V3" and by["C1"].support_for == "U1"
    assert by["R1"].support_for == "U1" and by["R1"].support_pin == "4"
    assert rails[0].name == "+3V3" and rails[0].max_current_ma == 100
    assert netlist.net("GND") is not None and netlist.net("GND").net_class.value == "ground"  # type: ignore[union-attr]
    plan = DesignPlan(prompt="t", components=annotated, netlist=netlist, power_tree=rails)
    assert repair_problems(plan, wiring=True) == []
    assert repair_problems(plan, wiring=False) == []


def test_intent_errors(library: ComponentLibrary) -> None:
    comps = _resolve(_manifest(), library).components
    bad = _intent().model_copy(deep=True)
    bad.nets[0].pins.append("U1.99")  # no such pin
    bad.nets[2].pins.append("U1.5")  # SCL pin also on SDA -> short
    bad.nets[1].pins.remove("U1.1")  # A1 left floating
    bad.no_connect.append(bad.no_connect[0].model_copy(update={"pin": "U1.6"}))  # VS as NC
    bad.nets[0].voltage_v = None
    bad.rails = []
    errs = " | ".join(validate_intent(bad, comps).errors)
    assert "U1 has no pin 99; its pins are [1=A1" in errs
    assert "U1.5 is in nets I2C_SDA and I2C_SCL" in errs
    assert "U1: pins 1(A1) are in no net" in errs
    assert "U1.6 is no-connect but is in net +3V3" in errs
    assert "power net +3V3 has no voltage" in errs


def test_pin_name_hint(library: ComponentLibrary) -> None:
    comps = _resolve(_manifest(), library).components
    bad = _intent().model_copy(deep=True)
    bad.nets[2].pins[1] = "U1.SDA"
    errs = validate_intent(bad, comps).errors
    assert any("pin name SDA is pin number 4" in e for e in errs)


def test_erc_feedback_splits_wiring_from_parts(library: ComponentLibrary) -> None:
    m = _manifest()
    m.parts = [p for p in m.parts if p.ref not in ("R1", "C1")]  # no SDA pull-up, no decoupling
    comps = _resolve(m, library).components
    it = _intent().model_copy(deep=True)
    it.nets[0].pins = ["J1.1", "U1.6", "R2.1"]
    it.nets[1].pins = ["J1.2", "U1.7", "U1.1", "U1.2"]
    it.nets[2].pins = ["J1.3", "U1.4"]
    assert validate_intent(it, comps).ok
    netlist, rails, annotated = build_netlist(it, comps)
    plan = DesignPlan(prompt="t", components=annotated, netlist=netlist, power_tree=rails)
    part_level = " ".join(repair_problems(plan, wiring=False))
    assert "ERC-DECOUPLING" in part_level and "ERC-I2C-PULLUP" in part_level
    assert repair_problems(plan, wiring=True) == []


def test_pin_dossier_lists_real_pins(library: ComponentLibrary) -> None:
    text = pin_dossier(_resolve(_manifest(), library).components, library)
    assert "U1 INA226 (INA226AIDGSR) [sensor_env; KiCad Sensor_Energy:INA226]" in text
    assert "6=VS/power_in" in text and "4=SDA/bidir[i2c_sda]" in text


# -- the tool loop ----------------------------------------------------------------------


class Scripted:
    """A tool-capable client that replays scripted turns and records what it was sent."""

    mode = "mock"
    model_name = "scripted"

    def __init__(self, turns: list[list[tuple[str, dict[str, Any]]]]) -> None:
        self.turns = turns
        self.requests: list[ToolRequest] = []

    def complete(self, request: Any) -> Any:  # pragma: no cover - not used
        raise AssertionError

    def complete_tools(self, request: ToolRequest) -> ToolTurn:
        self.requests.append(request)
        calls = tuple(
            ToolCall(f"t{len(self.requests)}_{i}", n, a)
            for i, (n, a) in enumerate(self.turns.pop(0))
        )
        return ToolTurn(
            text="",
            calls=calls,
            content=[
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
            ],
            model="scripted",
            stop_reason="tool_use",
        )


def test_tool_loop_looks_up_then_repairs(library: ComponentLibrary) -> None:
    good = _manifest()
    bad = good.model_copy(deep=True)
    bad.parts[0] = bad.parts[0].model_copy(update={"lib_id": "Sensor:INA226"})
    client = Scripted(
        [
            [("search_symbols", {"query": "INA226"})],
            [("submit", {"json": bad.model_dump_json()})],
            [("submit", {"json": "{not json"})],
            [("submit", {"json": good.model_dump_json()})],
        ]
    )
    audit = AuditLog(None)
    res = run_validated_stage(
        client,  # type: ignore[arg-type]
        stage="parts",
        output_model=PartsManifest,
        system="sys",
        user="design it",
        validate=lambda m: _resolve(m, library).errors,
        tools=lookup_tools(Libraries(library)),
        audit=audit,
    )
    assert res.accepted and res.submissions == 3 and res.tool_calls == 1
    # the lookup result, the validator finding and the schema error all went back to the model
    sent = json.dumps([m for m in client.requests[-1].messages if m["role"] == "user"][1:])
    assert "Sensor_Energy:INA226" in sent and "installed as" in sent
    assert "Schema validation failed" in sent and "is_error" in sent
    assert sum(1 for e in audit.entries if e.kind == "llm_call") == 4


def test_tool_loop_keeps_best_when_budget_runs_out(library: ComponentLibrary) -> None:
    bad = _manifest()
    bad.parts[0] = bad.parts[0].model_copy(update={"lib_id": "Sensor:INA226"})
    client = Scripted([[("submit", {"json": bad.model_dump_json()})]] * 2)
    res = run_validated_stage(
        client,  # type: ignore[arg-type]
        stage="parts",
        output_model=PartsManifest,
        system="s",
        user="u",
        validate=lambda m: _resolve(m, library).errors,
        max_submissions=2,
    )
    assert not res.accepted and res.open_problems and res.value.parts[0].ref == "U1"


def test_unknown_tool_and_tool_errors_do_not_end_the_stage(library: ComponentLibrary) -> None:
    def boom(_: dict[str, Any]) -> str:
        raise RuntimeError("kaput")

    client = Scripted(
        [
            [("nope", {}), ("boom", {})],
            [("submit", {"json": _manifest().model_dump_json()})],
        ]
    )
    res = run_validated_stage(
        client,  # type: ignore[arg-type]
        stage="parts",
        output_model=PartsManifest,
        system="s",
        user="u",
        validate=lambda m: [],
        tools=[Tool(ToolSpec("boom", "x", {"type": "object"}), boom)],
    )
    assert res.accepted
    results = client.requests[1].messages[-1]["content"]
    assert "unknown tool nope" in json.dumps(results) and "tool error: kaput" in json.dumps(results)


# -- whole pipeline -------------------------------------------------------------------


def test_intent_example_with_kicad_only_part(library: ComponentLibrary) -> None:
    plan = generate_plan(
        example_prompt("motor_current_monitor"), client=MockLLMClient(), lib=library
    )
    assert plan.engine == "intent" and plan.manifest is not None and plan.netlist_intent is not None
    u5 = plan.component("U5")
    assert u5 is not None and u5.kicad_symbol == "Sensor_Energy:INA226" and u5.rail == "+3V3"
    assert plan.check_summary is not None and plan.check_summary.errors == 0
    assert plan.netlist is not None and {"U5.9", "R6.2", "U4.5"} <= {
        f"{c.ref}.{c.pin}"
        for c in plan.netlist.net("VMOT").connections  # type: ignore[union-attr]
    }


def test_engines_agree_on_curated_example(library: ComponentLibrary) -> None:
    prompt = example_prompt("iot_env_sensor")
    a = generate_plan(prompt, client=MockLLMClient(), lib=library)
    b = generate_plan(
        prompt, client=MockLLMClient(), lib=library, options=PipelineOptions(engine="synth")
    )
    assert a.netlist is not None and b.netlist is not None
    conn = lambda p: {(n.name, c.ref, c.pin) for n in p.netlist.nets for c in n.connections}  # noqa: E731
    assert conn(a) == conn(b)
    errs = lambda p: {f.id for f in p.findings if f.severity.value != "info"}  # noqa: E731
    assert errs(a) == errs(b)


def test_part_level_erc_loops_back_to_parts(library: ComponentLibrary, tmp_path: Any) -> None:
    """A netlist that needs a part sends the parts stage round again (bounded)."""
    import shutil

    from pcbassist.paths import fixtures_dir

    root = tmp_path / "llm"
    shutil.copytree(fixtures_dir() / "llm", root)
    name = "iot_env_sensor"
    pf = root / "parts" / f"{name}.json"
    doc = json.loads(pf.read_text())
    good = doc["responses"][0]
    stripped = json.loads(json.dumps(good))
    stripped["parts"] = [p for p in stripped["parts"] if p["ref"] != "R8"]  # an I2C pull-up
    doc["responses"] = [stripped, good]
    pf.write_text(json.dumps(doc))
    nf = root / "netlist" / f"{name}.json"
    ndoc = json.loads(nf.read_text())
    full = ndoc["responses"][0]
    cut = json.loads(json.dumps(full))
    for n in cut["nets"]:
        n["pins"] = [p for p in n["pins"] if not p.startswith("R8.")]
    ndoc["responses"] = [cut, full]
    nf.write_text(json.dumps(ndoc))
    audit = AuditLog(None)
    plan = generate_plan(example_prompt(name), client=MockLLMClient(root), lib=library, audit=audit)
    stages = [e.stage for e in audit.entries if e.kind == "llm_call"]
    assert stages.count("parts") == 2 and stages.count("netlist") == 2
    assert plan.component("R8") is not None
    assert not any(
        f.rule == "ERC-I2C-PULLUP" and f.severity.value == "error" for f in plan.findings
    )


def test_index_builder_parses_units_extends_and_footprints() -> None:
    from pcbassist.kicad.libindex import parse_footprint_record, parse_symbol_lib

    lib = """(kicad_symbol_lib (version 20231120)
  (symbol "OPA" (property "Reference" "U") (property "Value" "OPA")
    (property "Footprint" "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
    (property "Datasheet" "https://example.com/opa.pdf") (property "Description" "dual op amp")
    (property "ki_keywords" "opamp") (property "ki_fp_filters" "SOIC*3.9x4.9mm*")
    (symbol "OPA_1_1" (pin output line (at 0 0 0) (length 2) (name "~" ) (number "1")))
    (symbol "OPA_1_2" (pin output line (at 0 0 0) (length 2) (name "X" ) (number "1")))
    (symbol "OPA_2_1" (pin output line (at 0 0 0) (length 2) (name "~" ) (number "7")))
    (symbol "OPA_3_1" (pin power_in line (at 0 0 0) (length 2) (name "V+") (number "8"))
                      (pin power_in line (at 0 0 0) (length 2) (name "V-") (number "4"))))
  (symbol "OPA2" (extends "OPA") (property "Value" "OPA2") (property "Datasheet" "~"))
  (symbol "GND" (power) (property "Reference" "#PWR") (property "Value" "GND")
    (symbol "GND_0_1" (pin power_in line (at 0 0 0) (length 0) (name "GND") (number "1")))))
"""
    recs = {r["id"]: r for r in parse_symbol_lib(lib, "Amp")}
    opa = recs["Amp:OPA"]
    assert opa["u"] == 3 and [p[0] for p in opa["p"]] == ["1", "7", "8", "4"]
    assert opa["ds"] == "https://example.com/opa.pdf" and opa["ff"] == ["SOIC*3.9x4.9mm*"]
    assert recs["Amp:OPA2"]["p"] == opa["p"] and recs["Amp:OPA2"]["ds"] == ""
    assert recs["Amp:OPA2"]["v"] == "OPA2" and recs["Amp:GND"].get("pw") is True

    fp = """(footprint "R_0402" (layer "F.Cu") (descr "chip resistor") (tags "resistor") (attr smd)
  (fp_rect (start -0.9 -0.5) (end 0.9 0.5) (layer "F.CrtYd"))
  (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu"))
  (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu")))"""
    rec = parse_footprint_record(fp, "Resistor_SMD:R_0402")
    assert rec["a"] == "smd" and rec["c"] == [-0.9, -0.5, 0.9, 0.5] and len(rec["p"]) == 2
    assert rec["d"] == "chip resistor" and rec["t"] == "resistor"
