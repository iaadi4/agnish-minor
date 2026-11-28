#!/usr/bin/env python3
"""Mock fixtures for the ``parts`` and ``netlist`` stages of the intent engine.

HONESTY NOTE: like ``author_fixtures.py``, these are not model recordings. They stand in for
what the live model is asked to submit, and are *derived from the deterministic
synthesizer's output* for each example (``--engine synth``), converted into the parts
manifest and netlist-intent shapes. Mock mode then pushes them through exactly the same
validators, ERC repair loop and downstream stages as a live run.

    uv run python scripts/derive_intent_fixtures.py     (run after author_fixtures.py)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("PCBASSIST_KICAD_FOOTPRINT_DIR", "none")

from author_fixtures import EXAMPLES, REFINE_FEEDBACK  # noqa: E402
from pcbassist.llm import MockLLMClient  # noqa: E402
from pcbassist.llm.mock import fixture_hash  # noqa: E402
from pcbassist.pipeline import PipelineOptions, generate_plan  # noqa: E402
from pcbassist.pipeline.engine import fixture_key, run_pipeline  # noqa: E402
from pcbassist.schemas import DesignPlan, NetClassName  # noqa: E402
from pcbassist.schemas.intent import NetlistIntent, PartsManifest  # noqa: E402

LIB = _lib_global = None  # set in main()


def manifest_of(plan: DesignPlan) -> dict[str, Any]:
    assert plan.architecture is not None
    blocks = {c.ref_des: c.block_id for c in plan.components}
    first_block = plan.architecture.blocks[0].id
    parts = []
    for c in plan.components:
        block = c.block_id or blocks.get(c.support_for or "") or first_block
        value = c.value or c.mpn
        if len(value) > 40:
            value = value[:40]
        e: dict[str, Any] = {
            "ref": c.ref_des,
            "value": value,
            "block_id": block,
            "role": c.role,
            "mpn": c.mpn,
        }
        if c.support_for:
            e["support_for"] = c.support_for
        if c.rationale:
            e["rationale"] = c.rationale
        lp = LIB.get(c.mpn)
        if lp is not None and c.current != lp.current:
            # design-specific load current the synthesizer computed (LED, motor, divider)
            e["ratings"] = {
                k: v
                for k, v in {
                    "typ_ma": c.current.typ_ma,
                    "max_ma": c.current.max_ma,
                    "sleep_ua": c.current.sleep_ua,
                }.items()
                if v is not None
            }
        if not c.verified:
            sym = unverified_symbol(c.mpn)
            if sym is None:
                e["off_board"] = True  # no KiCad symbol either: nothing to wire
            else:
                # outside the curated library: the stock KiCad symbol and its default footprint
                e["lib_id"], e["footprint"] = sym.lib_id, sym.footprint
        parts.append(e)
    doc = {"parts": parts, "notes": [n for n in plan.design_notes if len(n) < 300][:12]}
    PartsManifest.model_validate(doc)
    return doc


def intent_of(plan: DesignPlan) -> dict[str, Any]:
    assert plan.netlist is not None
    nets = []
    for n in plan.netlist.nets:
        d: dict[str, Any] = {"name": n.name, "pins": [f"{c.ref}.{c.pin}" for c in n.connections]}
        if n.net_class == NetClassName.GROUND:
            d["kind"] = "ground"
        elif n.net_class == NetClassName.POWER:
            d["kind"] = "power"
            d["voltage_v"] = n.voltage_v
        else:
            d["kind"] = "signal"
            if n.net_class != NetClassName.SIGNAL:
                d["net_class"] = n.net_class.value
        nets.append(d)
    nc = [{"pin": f"{x.ref}.{x.pin}", "reason": x.reason} for x in plan.netlist.no_connects]
    for c in plan.components:
        sym = None if c.verified else unverified_symbol(c.mpn)
        for p in sym.pins if sym else ():
            nc.append(
                {
                    "pin": f"{c.ref_des}.{p.number}",
                    "reason": "left unwired: the synthesizer only "
                    "wires curated parts (this example keeps the gap on purpose)",
                }
            )
    rails = [
        {
            "net": r.name,
            "voltage_v": r.nominal_v,
            "source_ref": r.source_ref,
            "source_kind": r.source_kind,
            "parent_net": r.parent_rail,
            "max_current_ma": r.max_current_ma,
            "min_v": r.min_v,
            "max_v": r.max_v,
        }
        for r in plan.power_tree
        if plan.netlist.net(r.name) is not None
    ]
    doc = {"nets": nets, "no_connect": nc, "rails": rails}
    NetlistIntent.model_validate(doc)
    return doc


def unverified_symbol(mpn: str) -> Any:
    from pcbassist.kicad.symbols import default_symbol_index

    hits = default_symbol_index().search(mpn, limit=1)
    return hits[0] if hits and hits[0].footprint else None


def write(stage: str, name: str, key: str, match: list[str], response: dict[str, Any]) -> None:
    out = ROOT / "fixtures" / "llm" / stage / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "_note": "Derived fixture (not a model recording): the synthesizer's design for this "
        "example in the shape the model submits. See scripts/derive_intent_fixtures.py.",
        "match": match,
        "prompt_sha256": fixture_hash(key),
        "responses": [response],
    }
    out.write_text(json.dumps(doc, indent=1) + "\n")


def main() -> None:
    global LIB
    LIB = _lib()
    synth = PipelineOptions(engine="synth")
    for name, ex in EXAMPLES.items():
        prompt = (ROOT / "examples" / name / "prompt.txt").read_text().strip()
        plan = generate_plan(prompt, client=MockLLMClient(), options=synth)
        write("parts", name, prompt, ex["match"], manifest_of(plan))
        write("netlist", name, prompt, ex["match"], intent_of(plan))
        if name == "iot_env_sensor":
            write("parts", "_default", "", [], manifest_of(plan))
            write("netlist", "_default", "", [], intent_of(plan))
            fb = plan.model_copy(deep=True)
            fb.feedback_history = [REFINE_FEEDBACK]
            v2 = run_pipeline(
                fb, start="components", client=MockLLMClient(), lib=None or _lib(), options=synth
            )
            key = fixture_key(prompt, [REFINE_FEEDBACK])
            match = ["bme280", "barometric"]
            write("parts", "iot_env_sensor_refine_bme280", key, match, manifest_of(v2))
            write("netlist", "iot_env_sensor_refine_bme280", key, match, intent_of(v2))
    current_monitor()
    print("intent fixtures written")


# ---------------------------------------------------------------------------
# Example 5: the relay/motor controller plus an INA226 current monitor. The INA226 is NOT in
# the curated library: it is chosen from the stock KiCad symbol Sensor_Energy:INA226 and
# wired pin by pin, which only the intent engine can do. Its ratings are datasheet claims
# (INA226 datasheet: 2.7-5.5 V supply, 330 uA typ / 420 uA max quiescent, 2.3 uA shutdown).
# ---------------------------------------------------------------------------
CM_MATCH = ["ina226", "current monitor", "power monitor"]


def _fixture(stage: str, name: str) -> Any:
    return json.loads((ROOT / "fixtures" / "llm" / stage / f"{name}.json").read_text())[
        "responses"
    ][0]


def current_monitor() -> None:
    from pcbassist.schemas import Architecture, Requirements

    name = "motor_current_monitor"
    prompt = (ROOT / "examples" / name / "prompt.txt").read_text().strip()
    req = _fixture("requirements", "relay_motor_12v")
    req["summary"] = "12 V Wi-Fi motor/relay controller with an INA226 motor-current monitor."
    req["functional_goals"].append("measure motor supply current and power with an INA226")
    req["sensors"] = [
        {
            "quantity": "current",
            "accuracy": "0.1 % gain error (INA226)",
            "notes": "high-side shunt on the motor supply",
        }
    ]
    req["interfaces"].append(
        {"kind": "I2C", "purpose": "INA226 current monitor", "external": False}
    )
    req["assumptions"].append(
        {
            "text": "50 mOhm shunt: 25 mV at 0.5 A, well inside the INA226 81.92 mV range",
            "field": "sensors",
        }
    )
    Requirements.model_validate(req)
    arch = _fixture("architecture", "relay_motor_12v")
    arch["blocks"].append(
        {"id": "imon", "kind": "sensor", "name": "Motor current monitor", "covers": ["current"]}
    )
    arch["links"] += [
        {"src": "power_in", "dst": "imon", "kind": "power", "label": "VIN 12 V"},
        {"src": "imon", "dst": "motor", "kind": "power", "label": "VMOT"},
        {"src": "mcu", "dst": "imon", "kind": "i2c"},
    ]
    Architecture.model_validate(arch)
    comps = _fixture("components", "relay_motor_12v")  # synth engine: curated parts only

    parts = _fixture("parts", "relay_motor_12v")
    parts["parts"] += [
        {
            "ref": "U5",
            "value": "INA226",
            "block_id": "imon",
            "role": "motor current monitor",
            "mpn": "INA226AIDGSR",
            "manufacturer": "Texas Instruments",
            "lib_id": "Sensor_Energy:INA226",
            "footprint": "Package_SO:VSSOP-10_3x3mm_P0.5mm",
            "rationale": "high-side current/power monitor with I2C; not in the curated library",
            "ratings": {
                "supply_min_v": 2.7,
                "supply_max_v": 5.5,
                "typ_ma": 0.33,
                "max_ma": 0.42,
                "sleep_ua": 2.3,
            },
        },
        {
            "ref": "R6",
            "value": "50m 1W",
            "block_id": "imon",
            "role": "motor current shunt",
            "lib_id": "Device:R",
            "footprint": "Resistor_SMD:R_2512_6332Metric",
            "rationale": "25 mV at 0.5 A; 12.5 mW dissipation",
        },
        {
            "ref": "C12",
            "value": "100nF 16V",
            "block_id": "imon",
            "role": "U5 VS decoupling",
            "package": "0402",
            "support_for": "U5",
        },
        {
            "ref": "R7",
            "value": "4.7k",
            "block_id": "imon",
            "role": "I2C SDA pull-up",
            "package": "0402",
            "support_for": "U5",
        },
        {
            "ref": "R8",
            "value": "4.7k",
            "block_id": "imon",
            "role": "I2C SCL pull-up",
            "package": "0402",
            "support_for": "U5",
        },
    ]
    parts["notes"] = ["INA226 at I2C address 0x40 (A0 = A1 = GND)."]
    PartsManifest.model_validate(parts)

    net = _fixture("netlist", "relay_motor_12v")
    by = {n["name"]: n for n in net["nets"]}
    by["VIN"]["pins"] = [p for p in by["VIN"]["pins"] if p not in ("C10.1", "C11.1", "U4.5")]
    by["VIN"]["pins"] += ["R6.1", "U5.10"]
    by["GND"]["pins"] += ["U5.7", "U5.1", "U5.2", "C12.2"]
    by["+3V3"]["pins"] += ["U5.6", "C12.1", "R7.1", "R8.1"]
    net["nets"] += [
        {
            "name": "VMOT",
            "kind": "power",
            "voltage_v": 12.0,
            "pins": ["R6.2", "U5.9", "U5.8", "U4.5", "C10.1", "C11.1"],
        },
        {"name": "I2C_SDA", "kind": "signal", "pins": ["U5.4", "R7.2", "U3.33"]},
        {"name": "I2C_SCL", "kind": "signal", "pins": ["U5.5", "R8.2", "U3.36"]},
    ]
    net["no_connect"] = [x for x in net["no_connect"] if x["pin"] not in ("U3.33", "U3.36")]
    net["no_connect"].append({"pin": "U5.3", "reason": "ALERT unused; firmware polls"})
    net["rails"].append(
        {
            "net": "VMOT",
            "voltage_v": 12.0,
            "source_ref": "R6",
            "source_kind": "other",
            "parent_net": "VIN",
            "min_v": 11.0,
            "max_v": 13.0,
        }
    )
    NetlistIntent.model_validate(net)
    for stage, doc in (
        ("requirements", req),
        ("architecture", arch),
        ("components", comps),
        ("parts", parts),
        ("netlist", net),
    ):
        write(stage, name, prompt, CM_MATCH, doc)


def _lib() -> Any:
    from pcbassist.library import ComponentLibrary

    return ComponentLibrary.load()


if __name__ == "__main__":
    main()
