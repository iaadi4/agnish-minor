from __future__ import annotations

from collections import Counter

import pytest

from conftest import EXAMPLES, example_prompt
from pcbassist.library import ComponentLibrary
from pcbassist.llm import MockLLMClient
from pcbassist.pipeline import Selected, run_pipeline, synthesize
from pcbassist.pipeline.engine import fixture_key
from pcbassist.pipeline.stages import (
    BLOCK_CATEGORIES,
    architecture_findings,
    candidates_for,
    requirements_findings,
    resolve_selection,
)
from pcbassist.pipeline.synth import parse_i2c_addresses, rail_name_for
from pcbassist.schemas import (
    Architecture,
    Block,
    ComponentSelection,
    DesignPlan,
    Requirements,
    SelectedPart,
    Severity,
)
from pcbassist.schemas.requirements import PowerSource


def _pins_of(plan: DesignPlan, ref: str) -> dict[str, str]:
    """pin -> net name for one component."""
    assert plan.netlist is not None
    out: dict[str, str] = {}
    for n in plan.netlist.nets:
        for c in n.connections:
            if c.ref == ref:
                out[c.pin] = n.name
    return out


def _ref(plan: DesignPlan, mpn: str) -> str:
    return next(c.ref_des for c in plan.components if c.mpn == mpn)


@pytest.mark.parametrize("name", EXAMPLES)
def test_every_pin_accounted_exactly_once(plans: dict[str, DesignPlan], name: str) -> None:
    plan = plans[name]
    assert plan.netlist is not None
    seen: Counter[tuple[str, str]] = Counter()
    for n in plan.netlist.nets:
        for c in n.connections:
            seen[(c.ref, c.pin)] += 1
    for nc in plan.netlist.no_connects:
        seen[(nc.ref, nc.pin)] += 1
    for comp in plan.components:
        if not comp.verified:
            assert not any(k[0] == comp.ref_des for k in seen), "unverified part in netlist"
            continue
        for p in comp.pins:
            assert seen[(comp.ref_des, p.number)] == 1, f"{name} {comp.ref_des}.{p.number}"
    refs = [c.ref_des for c in plan.components]
    assert len(refs) == len(set(refs))


@pytest.mark.parametrize("name", EXAMPLES)
def test_plans_have_provenance_and_disclaimer(plans: dict[str, DesignPlan], name: str) -> None:
    plan = plans[name]
    assert plan.llm is not None and plan.llm.mode == "mock"
    assert name in (plan.llm.fixture_set or "")
    assert "NOT VERIFIED FOR FABRICATION" in plan.disclaimer
    assert plan.requirements is not None and plan.requirements.assumptions
    assert not any(f.rule == "MOCK-NEAREST" for f in plan.findings)


def test_iot_core_connections(plans: dict[str, DesignPlan]) -> None:
    plan = plans["iot_env_sensor"]
    mcu = _ref(plan, "ESP32-C3-WROOM-02-N4")
    pins = _pins_of(plan, mcu)
    assert pins["1"] == "+3V3" and pins["9"] == "GND"
    assert pins["14"] == "USB_DP" and pins["13"] == "USB_DM"
    assert pins["2"] == "MCU_EN"
    # decoupling caps are tied to the MCU supply pin
    dec = [c for c in plan.components if c.support_for == mcu and c.role.startswith("Decoupling")]
    assert {c.value for c in dec} == {"10u", "100n"}
    # CC pull-downs: 5.1k to GND on both CC lines
    assert plan.netlist is not None
    for cc in ("USB_CC1", "USB_CC2"):
        net = plan.netlist.net(cc)
        assert net is not None
        res = [c.ref for c in net.connections if c.ref.startswith("R")]
        assert len(res) == 1
        assert plan.component(res[0]).value == "5.1k"  # type: ignore[union-attr]
        assert _pins_of(plan, res[0])["2"] == "GND"
    # I2C bus has exactly one pull-up pair
    for bus in ("I2C_SDA", "I2C_SCL"):
        net = plan.netlist.net(bus)
        assert net is not None
        pullups = [c.ref for c in net.connections if c.ref.startswith("R")]
        assert len(pullups) == 1
        assert _pins_of(plan, pullups[0])["1"] == "+3V3"
    # LDO runs from the battery; rail tree recorded
    rails = {r.name: r for r in plan.power_tree}
    assert rails["+3V3"].parent_rail == "VBAT" and rails["+3V3"].source_kind == "ldo"
    charger = plan.component(_ref(plan, "MCP73831T-2ACI/OT"))
    assert charger is not None and charger.key_specs["charge_current_ma"] == 500


def test_ble_tag_runs_from_coin_cell(plans: dict[str, DesignPlan]) -> None:
    plan = plans["ble_coin_tag"]
    mcu = _ref(plan, "MDBT42Q-512KV2")
    pins = _pins_of(plan, mcu)
    assert pins["11"] == "VBAT"
    assert pins["36"] == "SWCLK" and pins["37"] == "SWDIO" and pins["35"] == "MCU_RESET"
    acc = _ref(plan, "LIS3DHTR")
    apins = _pins_of(plan, acc)
    assert apins["8"] == "VBAT"  # CS high selects I2C
    assert apins["7"] == "GND"  # SA0 low -> 0x18
    assert apins["11"].startswith("INT_")  # INT1 to the MCU
    assert plan.component(acc).key_specs["i2c_address_assigned"] == "0x18"  # type: ignore[union-attr]
    assert {r.name for r in plan.power_tree} == {"VBAT"}


def test_relay_motor_power_chain(plans: dict[str, DesignPlan]) -> None:
    plan = plans["relay_motor_12v"]
    assert plan.netlist is not None
    fuse = _pins_of(plan, _ref(plan, "MF-MSMF110-2"))
    assert fuse == {"1": "VIN_RAW", "2": "VIN_F"}
    diode = plan.component(_ref(plan, "SS14"))
    assert diode is not None and diode.origin == "llm"
    assert _pins_of(plan, diode.ref_des) == {"2": "VIN_F", "1": "VIN"}
    relay = _ref(plan, "SRD-05VDC-SL-C")
    rp = _pins_of(plan, relay)
    assert rp["2"] == "+5V" and rp["5"] == "RELAY1_COIL"
    fly = _pins_of(plan, _ref(plan, "1N4148W-7-F"))
    assert fly == {"1": "+5V", "2": "RELAY1_COIL"}  # cathode to supply
    rails = {r.name: r for r in plan.power_tree}
    assert rails["+5V"].parent_rail == "VIN" and rails["+3V3"].parent_rail == "+5V"
    motor = _pins_of(plan, _ref(plan, "DRV8871DDAR"))
    assert motor["5"] == "VIN" and motor["6"] == "MOTOR1_OUT1"
    assert sum(1 for c in plan.components if c.mpn == "MOUNTING-HOLE-M3") == 4
    # 12 V caps are rated for it
    for c in plan.components:
        if c.ref_des.startswith("C") and any(
            n.name == "VIN" for n in plan.netlist.nets_of(c.ref_des, "1")
        ):
            assert float(c.key_specs["voltage_v"]) >= 18


def test_flawed_design_excludes_unverified_part(plans: dict[str, DesignPlan]) -> None:
    plan = plans["flawed_coin_wifi"]
    bad = next(c for c in plan.components if c.mpn == "MMBT3904")
    assert not bad.verified and bad.confidence <= 0.3 and not bad.pins
    assert any(f.rule == "LIB-UNVERIFIED" for f in plan.findings)
    sht = [c for c in plan.components if c.mpn == "SHT40-AD1B-R3"]
    assert [c.key_specs["i2c_address_assigned"] for c in sht] == ["0x44", "0x44"]
    assert sum(1 for f in plan.findings if f.rule == "REQ-CONFLICT") == 3


def test_second_instance_gets_alternate_address(library: ComponentLibrary) -> None:
    req = _req()
    sel = [
        Selected("ESP32-C3-WROOM-02-N4", "mcu", "mcu", "mcu_wireless"),
        Selected("AP2112K-3.3TRG1", "ldo", "reg", "regulation"),
        Selected("S2B-PH-SM4-TB(LF)(SN)", "batt", "bat", "battery"),
        Selected("BME280", "baro 1", "s", "sensor"),
        Selected("BME280", "baro 2", "s", "sensor"),
    ]
    res = synthesize(library, req, sel)
    addrs = [c.key_specs["i2c_address_assigned"] for c in res.components if c.mpn == "BME280"]
    assert addrs == ["0x76", "0x77"]


def test_unsupported_part_is_left_explicitly_incomplete(library: ComponentLibrary) -> None:
    sel = [
        Selected("ESP32-C3-WROOM-02-N4", "mcu", "mcu", "mcu_wireless"),
        Selected("S2B-PH-SM4-TB(LF)(SN)", "batt", "bat", "battery"),
        Selected("TPS563200DDCR", "buck", "reg", "regulation"),
    ]
    res = synthesize(library, _req(), sel)
    f = next(f for f in res.findings if f.rule == "SYNTH-INCOMPLETE")
    assert f.severity == Severity.WARNING and "TPS563200DDCR" in f.message
    buck = next(c for c in res.components if c.mpn == "TPS563200DDCR")
    reasons = {nc.reason for nc in res.netlist.no_connects if nc.ref == buck.ref_des}
    assert any("engineer must complete" in r for r in reasons)


def test_previous_refs_are_reused(library: ComponentLibrary) -> None:
    sel = [
        Selected("S2B-PH-SM4-TB(LF)(SN)", "batt", "bat", "battery"),
        Selected("ESP32-C3-WROOM-02-N4", "mcu", "mcu", "mcu_wireless"),
    ]
    first = synthesize(library, _req(), sel)
    mcu = next(c for c in first.components if c.mpn.startswith("ESP32"))
    second = synthesize(library, _req(), sel, previous_refs={mcu.key or "": "U7"})
    assert next(c for c in second.components if c.mpn.startswith("ESP32")).ref_des == "U7"


def _req() -> Requirements:
    return Requirements(
        summary="t",
        power_sources=[
            PowerSource(kind="lipo_1s", nominal_v=3.7, min_v=3.0, max_v=4.2, capacity_mah=500)
        ],
    )


def test_rail_names_and_address_parsing() -> None:
    assert rail_name_for(3.3) == "+3V3"
    assert rail_name_for(5.0) == "+5V"
    assert rail_name_for(1.8) == "+1V8"
    assert parse_i2c_addresses("0x44") == [0x44]
    assert parse_i2c_addresses("0x76/0x77 (SDO)") == [0x76, 0x77]
    assert parse_i2c_addresses("0x48-0x4B (ADD0)") == [0x48, 0x49, 0x4A, 0x4B]
    assert parse_i2c_addresses(None) == []


def _arch() -> Architecture:
    return Architecture(
        blocks=[
            Block(id="mcu", kind="mcu_wireless", name="MCU"),
            Block(id="sensor", kind="sensor", name="S"),
        ]
    )


def test_resolve_selection_cross_checks(library: ComponentLibrary) -> None:
    sel = ComponentSelection(
        parts=[
            SelectedPart(
                role="mcu",
                block_id="mcu",
                mpn="esp32-c3-wroom-02-n4",
                rationale="x",
                confidence=0.9,
            ),
            SelectedPart(
                role="pull-up",
                block_id="sensor",
                mpn="RC0402FR-0710KL",
                rationale="x",
                confidence=0.9,
            ),
            SelectedPart(
                role="made up", block_id="sensor", mpn="FOO-123", rationale="x", confidence=0.9
            ),
            SelectedPart(
                role="odd",
                block_id="sensor",
                mpn="SS14",
                rationale="x",
                confidence=0.9,
                alternates=["NOPE-1"],
            ),
            SelectedPart(
                role="ghost",
                block_id="nowhere",
                mpn="SHT40-AD1B-R3",
                rationale="x",
                confidence=0.9,
                quantity=2,
            ),
            SelectedPart(
                role="banned", block_id="sensor", mpn="BME280", rationale="x", confidence=0.9
            ),
        ]
    )
    out, findings = resolve_selection(sel, _arch(), library, rejected_mpns={"BME280"})
    rules = Counter(f.rule for f in findings)
    assert rules["SEL-PASSIVE-DROPPED"] == 1
    assert rules["SEL-OFF-CANDIDATE"] >= 1
    assert rules["SEL-UNKNOWN-BLOCK"] == 1
    assert rules["SEL-ALT-UNKNOWN"] == 1
    assert rules["SEL-REJECTED-AGAIN"] == 1
    mpns = [s.mpn for s in out]
    assert mpns.count("SHT40-AD1B-R3") == 2
    assert "ESP32-C3-WROOM-02-N4" in mpns  # normalised to the library spelling
    fake = next(s for s in out if s.mpn == "FOO-123")
    assert fake.confidence <= 0.3


def test_candidates_follow_block_kinds(library: ComponentLibrary) -> None:
    cands = candidates_for(_arch(), library)
    assert {p.category for p in cands["mcu"]} == {"mcu_wireless"}
    assert {p.category for p in cands["sensor"]} <= set(BLOCK_CATEGORIES["sensor"])


def test_architecture_coverage_findings() -> None:
    req = Requirements(
        summary="x",
        wireless=[{"protocol": "wifi"}],  # type: ignore[list-item]
        sensors=[{"quantity": "temperature"}],  # type: ignore[list-item]
        actuators=[{"kind": "relay"}],  # type: ignore[list-item]
        power_sources=[
            PowerSource(kind="lipo_1s", nominal_v=3.7, min_v=3, max_v=4.2, rechargeable=True)
        ],
    )
    arch = Architecture(blocks=[Block(id="x", kind="other", name="x")])
    subjects = {f.id.split(":")[1] for f in architecture_findings(req, arch)}
    assert subjects == {"wireless", "sensor", "actuator", "battery", "charging", "mcu"}
    assert not architecture_findings(Requirements(summary="x"), _arch())


def test_requirements_findings() -> None:
    req = Requirements(summary="x", conflicts=["a"], out_of_scope=["mains"])
    rules = sorted(f.rule for f in requirements_findings(req))
    assert rules == ["REQ-CONFLICT", "REQ-NO-POWER", "REQ-OUT-OF-SCOPE"]


def test_component_prompt_contains_candidates(library: ComponentLibrary) -> None:
    client = MockLLMClient()
    run_pipeline(
        DesignPlan(prompt=example_prompt("iot_env_sensor")),
        start="requirements",
        client=client,
        lib=library,
    )
    comp_call = next(c for c in client.calls if c.stage == "components")
    text = comp_call.messages[0].content
    assert "SHT40-AD1B-R3" in text and "## block `sensor`" in text
    assert "Do NOT choose resistors" in comp_call.system


def test_unknown_start_stage(library: ComponentLibrary) -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        run_pipeline(DesignPlan(prompt="x"), start="bogus", client=MockLLMClient(), lib=library)
    with pytest.raises(ValueError, match="no requirements"):
        run_pipeline(
            DesignPlan(prompt="x"), start="architecture", client=MockLLMClient(), lib=library
        )


def test_unmatched_prompt_uses_default_fixture_and_warns(library: ComponentLibrary) -> None:
    plan = run_pipeline(
        DesignPlan(prompt="a solar-powered weather station"),
        start="requirements",
        client=MockLLMClient(),
        lib=library,
    )
    assert any(f.rule == "MOCK-NEAREST" for f in plan.findings)


def test_fixture_key_includes_feedback() -> None:
    assert fixture_key("p", []) == "p"
    assert "feedback" in fixture_key("p", ["f"])
