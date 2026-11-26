from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbassist.checks import analyze_power, parse_drc, power_findings, run_checks, run_dfm, run_erc
from pcbassist.checks.dfm import passive_size
from pcbassist.checks.drc import load_drc_report
from pcbassist.checks.library import library_findings
from pcbassist.fab import list_fab_profiles, load_fab_profile
from pcbassist.schemas import CheckStatus, DesignPlan, NetConnection, NoConnect, Severity


def _rules(findings, sev: Severity | None = None) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.rule for f in findings if sev is None or f.severity == sev}


def _ids(findings) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.id for f in findings}


def _ref(plan: DesignPlan, mpn: str) -> str:
    return next(c.ref_des for c in plan.components if c.mpn == mpn)


def _drop_pin(plan: DesignPlan, ref: str, pin: str) -> str:
    assert plan.netlist is not None
    for n in plan.netlist.nets:
        before = len(n.connections)
        n.connections = [c for c in n.connections if not (c.ref == ref and c.pin == pin)]
        if len(n.connections) != before:
            return n.name
    raise AssertionError("pin not on a net")


# ---------------------------------------------------------------------------
# summary status of the examples
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["iot_env_sensor", "ble_coin_tag", "relay_motor_12v"])
def test_good_examples_have_no_errors(plans: dict[str, DesignPlan], name: str) -> None:
    plan = plans[name]
    assert plan.check_summary is not None
    errors = [f.id for f in plan.findings if f.severity == Severity.ERROR]
    assert errors == []
    assert plan.check_summary.status == CheckStatus.PASS_WITH_WARNINGS
    assert not plan.check_summary.kicad_drc_ran


def test_flawed_example_fails_for_the_right_reasons(plans: dict[str, DesignPlan]) -> None:
    plan = plans["flawed_coin_wifi"]
    assert plan.check_summary is not None and plan.check_summary.status == CheckStatus.FAIL
    errors = _rules(plan.findings, Severity.ERROR)
    expected = {
        "ERC-I2C-ADDRESS",  # two identical SHT40s at 0x44
        "ERC-SUPPLY-RANGE",  # 5 V relay coil on 3.3 V
        "PWR-DROPOUT",  # 3.3 V LDO from a 3.0 V coin cell
        "PWR-REG-CAPACITY",  # 250 mA LDO vs ~420 mA peak
        "PWR-SOURCE",  # coin cell vs Wi-Fi peaks
        "PWR-BATTERY-LIFE",  # 2 years at 10 s uploads
        "CON-BOARD-AREA",  # 15 x 15 mm
        "DFM-UNPLACED",
    }
    assert expected <= errors, expected - errors
    assert "LIB-UNVERIFIED" in _rules(plan.findings, Severity.WARNING)
    # findings are sorted errors first
    sev = [f.severity for f in plan.findings]
    assert sev == sorted(sev, key=lambda s: ["error", "warning", "info"].index(s.value))


# ---------------------------------------------------------------------------
# ERC rules, each triggered by mutating a clean netlist
# ---------------------------------------------------------------------------


def test_clean_netlist_has_no_erc_errors(plan_copy) -> None:  # type: ignore[no-untyped-def]
    for name in ("iot_env_sensor", "ble_coin_tag", "relay_motor_12v"):
        assert not _rules(run_erc(plan_copy(name)), Severity.ERROR), name


def test_erc_unconnected_power_pin(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    mcu = _ref(p, "ESP32-C3-WROOM-02-N4")
    _drop_pin(p, mcu, "1")
    f = next(f for f in run_erc(p) if f.id == f"ERC-UNCONNECTED:{mcu}.1")
    assert f.severity == Severity.ERROR


def test_erc_unconnected_signal_pin_is_warning(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    mcu = _ref(p, "ESP32-C3-WROOM-02-N4")
    p.netlist.no_connects = [nc for nc in p.netlist.no_connects if nc.ref != mcu]
    found = [f for f in run_erc(p) if f.rule == "ERC-UNCONNECTED"]
    assert found and all(f.severity == Severity.WARNING for f in found)


def test_erc_nc_and_multi_net_conflicts(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    mcu = _ref(p, "ESP32-C3-WROOM-02-N4")
    p.netlist.no_connects.append(NoConnect(ref=mcu, pin="1", reason="bogus"))
    p.netlist.net("GND").connections.append(NetConnection(ref=mcu, pin="1"))
    p.netlist.net("GND").connections.append(NetConnection(ref="U99", pin="1"))
    p.netlist.net("GND").connections.append(NetConnection(ref=mcu, pin="999"))
    rules = _rules(run_erc(p), Severity.ERROR)
    assert {"ERC-NC-CONNECTED", "ERC-MULTI-NET", "ERC-UNKNOWN-REF", "ERC-UNKNOWN-PIN"} <= rules


def test_erc_ground_pin_on_wrong_net(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    sensor = _ref(p, "SHT40-AD1B-R3")
    _drop_pin(p, sensor, "4")
    p.netlist.net("+3V3").connections.append(NetConnection(ref=sensor, pin="4"))
    assert f"ERC-GROUND-PIN:{sensor}.4" in _ids(run_erc(p))


def test_erc_single_pin_and_floating_input(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    mcu = _ref(p, "ESP32-C3-WROOM-02-N4")
    en = p.netlist.net("MCU_EN")
    en.connections = [c for c in en.connections if c.ref == mcu]
    found = _ids(run_erc(p))
    assert "ERC-SINGLE-PIN-NET:MCU_EN" in found
    assert "ERC-FLOATING-INPUT:MCU_EN" in found


def test_erc_driver_conflict(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    ldo = _ref(p, "AP2112K-3.3TRG1")
    _drop_pin(p, ldo, "5")
    p.netlist.net("VBAT").connections.append(NetConnection(ref=ldo, pin="5"))
    f = next(f for f in run_erc(p) if f.id == "ERC-DRIVER-CONFLICT:VBAT")
    assert f.severity == Severity.ERROR


def test_erc_undriven_power_net(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("ble_coin_tag")
    net = p.netlist.net("VBAT")
    net.connections = [
        c for c in net.connections if p.component(c.ref).pin(c.pin).type.value == "power_in"
    ]
    assert "ERC-POWER-UNDRIVEN:VBAT" in _ids(run_erc(p))


def test_erc_missing_decoupling(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    sensor = _ref(p, "SHT40-AD1B-R3")
    cap = next(c.ref_des for c in p.components if c.support_for == sensor)
    _drop_pin(p, cap, "1")
    f = next(f for f in run_erc(p) if f.id == f"ERC-DECOUPLING:{sensor}.3")
    assert f.severity == Severity.WARNING  # other caps on +3V3 still exist
    p2 = plan_copy("ble_coin_tag")
    for c in [c for c in p2.components if c.category == "capacitor"]:
        _drop_pin(p2, c.ref_des, "1")
    errs = [f for f in run_erc(p2) if f.rule == "ERC-DECOUPLING"]
    assert errs and all(f.severity == Severity.ERROR for f in errs)


def test_erc_i2c_pullups(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    sda = p.netlist.net("I2C_SDA")
    sda.connections = [c for c in sda.connections if not c.ref.startswith("R")]
    f = next(f for f in run_erc(p) if f.id == "ERC-I2C-PULLUP:I2C_SDA")
    assert f.severity == Severity.ERROR


def test_erc_usb_rules(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    esd = _ref(p, "USBLC6-2SC6")
    for pin in ("1", "6", "3", "4"):
        _drop_pin(p, esd, pin)
    cc = p.netlist.net("USB_CC1")
    cc.connections = [c for c in cc.connections if c.ref.startswith("J")]
    found = _ids(run_erc(p))
    j = _ref(p, "USB4105-GF-A")
    assert f"ERC-USB-ESD:{j}" in found
    assert f"ERC-USB-CC:{j}.A5" in found


def test_erc_flyback_led_and_cap_voltage(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("relay_motor_12v")
    diode = _ref(p, "1N4148W-7-F")
    _drop_pin(p, diode, "2")
    led = _ref(p, "LTST-C191KGKT")
    anode = _drop_pin(p, led, "2")
    del anode
    p.netlist.net("+3V3").connections.append(NetConnection(ref=led, pin="2"))
    cap = next(
        c for c in p.components if c.category == "capacitor" and c.key_specs["voltage_v"] == 10
    )
    _drop_pin(p, cap.ref_des, "1")
    p.netlist.net("VIN").connections.append(NetConnection(ref=cap.ref_des, pin="1"))
    found = _ids(run_erc(p))
    assert f"ERC-FLYBACK:{_ref(p, 'SRD-05VDC-SL-C')}" in found
    assert f"ERC-CAP-VOLTAGE:{cap.ref_des}" in found
    # LED anode straight onto +3V3: no series resistor on its own net
    assert f"ERC-LED-RESISTOR:{led}" in found


def test_erc_supply_range_brownout_warning(plans: dict[str, DesignPlan]) -> None:
    f = next(
        f
        for f in plans["flawed_coin_wifi"].findings
        if f.rule == "ERC-SUPPLY-RANGE" and f.severity == Severity.WARNING
    )
    assert "brown-out" in f.message


# ---------------------------------------------------------------------------
# power
# ---------------------------------------------------------------------------


def test_power_budget_numbers(plans: dict[str, DesignPlan]) -> None:
    rep = analyze_power(plans["iot_env_sensor"])
    b = rep.budgets
    assert b["+3V3"].peak_ma == pytest.approx(350 + 0.5 + 1.3, abs=0.2)
    # VBAT carries +3V3 plus the LDO quiescent current and the sense divider
    assert b["VBAT"].peak_ma > b["+3V3"].peak_ma
    assert rep.battery_life_days == pytest.approx(96, abs=3)
    assert rep.duty_fraction == pytest.approx(3 / 900)
    relay = analyze_power(plans["relay_motor_12v"]).budgets
    # buck input current is scaled by Vout/(Vin*eff)
    five = relay["+5V"].peak_ma
    assert relay["VIN"].peak_ma == pytest.approx(500 + five * 5 / (11.5 * 0.85) + 5, rel=0.05)


def test_power_thermal_and_charge_rate(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("relay_motor_12v")
    mcu = p.component(_ref(p, "ESP32-WROOM-32E-N4"))
    mcu.current.typ_ma = 300
    assert "PWR-THERMAL" in _rules(power_findings(p))
    q = plan_copy("iot_env_sensor")
    q.requirements.power_sources[0].capacity_mah = 300
    assert any(
        f.rule == "PWR-CHARGE-RATE" and f.severity == Severity.ERROR for f in power_findings(q)
    )
    q.requirements.power_sources[0].capacity_mah = 600
    assert any(
        f.rule == "PWR-CHARGE-RATE" and f.severity == Severity.WARNING for f in power_findings(q)
    )


def test_power_no_data_and_reg_input(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    sensor = p.component(_ref(p, "SHT40-AD1B-R3"))
    sensor.current.typ_ma = sensor.current.max_ma = sensor.current.sleep_ua = None
    assert f"PWR-NO-DATA:{sensor.ref_des}" in _ids(power_findings(p))
    for r in p.constraints.electrical.rails:
        if r.name == "VBAT":
            r.max_v = 7.0
    assert "PWR-REG-INPUT:+3V3" in _ids(power_findings(p))


def test_battery_life_levels(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    p.requirements.target_battery_life_days = 10
    f = next(f for f in power_findings(p) if f.rule == "PWR-BATTERY-LIFE")
    assert f.severity == Severity.INFO
    p.requirements.target_battery_life_days = 200
    f = next(f for f in power_findings(p) if f.rule == "PWR-BATTERY-LIFE")
    assert f.severity == Severity.ERROR


# ---------------------------------------------------------------------------
# DFM / library / DRC
# ---------------------------------------------------------------------------


def test_dfm_with_stricter_profile(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    fab = load_fab_profile("generic_2layer").model_copy(
        update={
            "min_passive_size": "0603",
            "min_pad_pitch_mm": 0.8,
            "allow_tht": False,
            "min_trace_mm": 0.3,
            "min_space_mm": 0.3,
            "min_via_drill_mm": 0.5,
        }
    )
    rules = _rules(run_dfm(p, fab), Severity.ERROR)
    assert {"DFM-PASSIVE-SIZE", "DFM-PAD-PITCH", "DFM-THT", "DFM-NETCLASS"} <= rules


def test_dfm_overlap_outside_and_edge(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    fab = load_fab_profile("generic_2layer")
    pl = {x.ref: x for x in p.placement.placements}
    a, b = pl["R1"], pl["R2"]
    b.x_mm, b.y_mm = a.x_mm, a.y_mm
    b.bbox = a.bbox
    pl["R3"].x_mm = -20
    pl["R3"].bbox = pl["R3"].bbox.model_copy(update={"x_mm": -21})
    rules = _rules(run_dfm(p, fab))
    assert {"DFM-COURTYARD-OVERLAP", "DFM-OUTSIDE-BOARD"} <= rules
    p.placement.placements = [x for x in p.placement.placements if x.ref != "R4"]
    assert "DFM-UNPLACED" in _rules(run_dfm(p, fab))


def test_passive_size_parsing() -> None:
    assert passive_size("Resistor_SMD:R_0402_1005Metric") == "0402"
    assert passive_size("Capacitor_SMD:C_0603_1608Metric") == "0603"
    assert passive_size("Package_TO_SOT_SMD:SOT-23") is None


def test_library_findings(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    p.components[0].footprint = "Nope:Missing"
    p.components[1].key_specs["data_confidence"] = "low"
    rules = _rules(library_findings(p))
    assert {"LIB-FOOTPRINT", "LIB-LOW-CONFIDENCE", "LIB-NO-DATASHEET"} <= rules


SAMPLE_DRC = {
    "kicad_version": "8.0.9",
    "violations": [
        {
            "type": "clearance",
            "severity": "error",
            "description": "Clearance violation",
            "items": [{"description": "Pad 1 of U1"}, {"description": "Pad 2 of C1"}],
        },
        {"type": "silk_overlap", "severity": "warning", "description": "Silk", "items": []},
    ],
    "unconnected_items": [
        {"type": "unconnected_items", "severity": "error", "description": "Missing", "items": []}
    ]
    * 6,
}


def test_drc_parsing(tmp_path: Path) -> None:
    f = parse_drc(SAMPLE_DRC)
    by = {x.rule: x for x in f}
    assert by["DRC-CLEARANCE"].severity == Severity.ERROR
    assert by["DRC-CLEARANCE"].source.value == "kicad"
    assert by["DRC-UNCONNECTED_ITEMS"].severity == Severity.INFO
    assert "+2 more" in by["DRC-UNCONNECTED_ITEMS"].message
    assert by["DRC-SILK_OVERLAP"].severity == Severity.INFO
    path = tmp_path / "drc.json"
    path.write_text(json.dumps(SAMPLE_DRC))
    assert len(load_drc_report(path)) == 3


def test_run_checks_merges_drc_and_is_idempotent(plan_copy, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    path = tmp_path / "drc.json"
    path.write_text(json.dumps(SAMPLE_DRC))
    run_checks(p, drc_report=path)
    assert p.check_summary.kicad_drc_ran and p.check_summary.status == CheckStatus.FAIL
    n = len(p.findings)
    run_checks(p, drc_report=path)
    assert len(p.findings) == n
    run_checks(p)
    assert not any(f.rule.startswith("DRC-") for f in p.findings)


def test_fab_profiles_load() -> None:
    assert list_fab_profiles() == ["generic_2layer", "generic_4layer"]
    with pytest.raises(ValueError, match="unknown fab profile"):
        load_fab_profile("nope")
