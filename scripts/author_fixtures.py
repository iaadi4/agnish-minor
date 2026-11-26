"""Source of the hand-authored mock-LLM fixtures (``fixtures/llm/<stage>/<example>.json``).

HONESTY NOTE: no API key was available while building pcbassist, so these responses were
written by hand in the exact JSON shape the live model is asked to produce. They are NOT
recordings of a model. Run ``python scripts/author_fixtures.py`` to regenerate the JSON
files (it also refreshes the exact-prompt hashes from ``examples/*/prompt.txt``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcbassist.llm.mock import fixture_hash  # noqa: E402
from pcbassist.pipeline.engine import fixture_key  # noqa: E402
from pcbassist.schemas import Architecture, ComponentSelection, Requirements  # noqa: E402


def A(text: str, field: str | None = None, why: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"text": text}
    if field:
        d["field"] = field
    if why:
        d["rationale"] = why
    return d


def part(
    role: str,
    block: str,
    mpn: str,
    why: str,
    conf: float,
    qty: int = 1,
    alts: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "role": role,
        "block_id": block,
        "mpn": mpn,
        "quantity": qty,
        "rationale": why,
        "confidence": conf,
        "alternates": alts or [],
    }


LIPO = {
    "kind": "lipo_1s",
    "nominal_v": 3.7,
    "min_v": 3.0,
    "max_v": 4.2,
    "capacity_mah": 1000,
    "max_current_ma": 1000,
    "rechargeable": True,
    "notes": "single cell with built-in protection PCB assumed",
}
USB = {
    "kind": "usb_5v",
    "nominal_v": 5.0,
    "min_v": 4.75,
    "max_v": 5.25,
    "max_current_ma": 500,
    "rechargeable": False,
    "notes": "USB 2.0 default power; no PD negotiation",
}
CR2032 = {
    "kind": "coin_cell",
    "nominal_v": 3.0,
    "min_v": 2.0,
    "max_v": 3.0,
    "capacity_mah": 220,
    "max_current_ma": 15,
    "rechargeable": False,
    "notes": "CR2032: about 3 mA continuous, 15 mA pulse",
}

EXAMPLES: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# 1. IoT environmental sensor
# ---------------------------------------------------------------------------
EXAMPLES["iot_env_sensor"] = {
    "match": ["wi-fi", "wifi", "temperature", "humidity", "lipo", "usb-c", "battery-powered"],
    "requirements": {
        "summary": "Battery-powered Wi-Fi temperature/humidity sensor with USB-C LiPo charging.",
        "functional_goals": [
            "measure temperature and relative humidity every 15 minutes",
            "report readings over Wi-Fi then deep-sleep",
            "charge a 1000 mAh LiPo from USB-C",
            "status LED and one user button",
        ],
        "power_sources": [LIPO, USB],
        "target_battery_life_days": 90,
        "duty_cycle": "wake every 15 min, about 3 s awake (sensor read + Wi-Fi report)",
        "duty_active_s": 3,
        "duty_period_s": 900,
        "voltage_rails": [
            {"name": "+3V3", "nominal_v": 3.3, "tolerance_pct": 3, "purpose": "MCU and sensor"}
        ],
        "interfaces": [
            {"kind": "USB", "purpose": "charging and firmware upload", "external": True},
            {"kind": "I2C", "purpose": "temperature/humidity sensor"},
        ],
        "wireless": [{"protocol": "wifi", "antenna": "pcb"}],
        "sensors": [
            {"quantity": "temperature", "accuracy": "+/-0.5 C"},
            {"quantity": "humidity", "accuracy": "+/-3 %RH"},
        ],
        "user_io": ["status LED", "user button"],
        "environment": {"temp_min_c": 0, "temp_max_c": 50, "notes": "indoor"},
        "size": {"max_width_mm": 50, "max_height_mm": 35},
        "layer_count_target": 2,
        "assumptions": [
            A("Indoor use, 0-50 C", "environment"),
            A("About 3 s awake per report (connect, measure, publish)", "duty_active_s"),
            A("LiPo has its own protection circuit (no on-board protector)", "power_sources"),
            A("2-layer board is sufficient", "layer_count_target"),
            A("USB is used for charging and native-USB programming", "interfaces"),
        ],
        "open_questions": [
            "Which cloud protocol/backend (MQTT, HTTP)?",
            "Enclosure and venting for accurate humidity readings?",
        ],
    },
    "architecture": {
        "blocks": [
            {"id": "usb_in", "kind": "power_input", "name": "USB-C input + ESD", "covers": ["usb"]},
            {"id": "charger", "kind": "charging", "name": "LiPo charger", "covers": ["charging"]},
            {"id": "battery", "kind": "battery", "name": "LiPo connector", "covers": ["battery"]},
            {"id": "regulator", "kind": "regulation", "name": "3.3 V LDO", "covers": ["+3V3"]},
            {"id": "mcu", "kind": "mcu_wireless", "name": "Wi-Fi MCU module", "covers": ["wifi"]},
            {
                "id": "sensor",
                "kind": "sensor",
                "name": "T/RH sensor",
                "covers": ["temperature", "humidity"],
            },
            {
                "id": "ui",
                "kind": "user_io",
                "name": "LED and button",
                "covers": ["status LED", "user button"],
            },
        ],
        "links": [
            {"src": "usb_in", "dst": "charger", "kind": "power", "label": "VBUS 5 V"},
            {"src": "charger", "dst": "battery", "kind": "power", "label": "charge"},
            {"src": "battery", "dst": "regulator", "kind": "power", "label": "VBAT 3.0-4.2 V"},
            {"src": "regulator", "dst": "mcu", "kind": "power", "label": "+3V3"},
            {"src": "regulator", "dst": "sensor", "kind": "power", "label": "+3V3"},
            {"src": "usb_in", "dst": "mcu", "kind": "usb", "label": "USB D+/D-"},
            {"src": "mcu", "dst": "sensor", "kind": "i2c"},
            {"src": "mcu", "dst": "ui", "kind": "gpio"},
        ],
        "rationale": "Single Wi-Fi SoC module; LDO from the battery so the board runs without USB.",
    },
    "components": {
        "parts": [
            part(
                "USB-C receptacle (charging + data)",
                "usb_in",
                "USB4105-GF-A",
                "USB 2.0 sink receptacle in the library",
                0.85,
                alts=["TYPE-C-31-M-12"],
            ),
            part(
                "USB ESD protection",
                "usb_in",
                "USBLC6-2SC6",
                "standard 2-line TVS array for D+/D- and VBUS",
                0.9,
            ),
            part(
                "LiPo linear charger",
                "charger",
                "MCP73831T-2ACI/OT",
                "single-cell 4.2 V charger, 500 mA max, tiny SOT-23-5",
                0.9,
                alts=["BQ24074RGTR"],
            ),
            part(
                "charge status LED",
                "charger",
                "LTST-C191KRKT",
                "red LED driven by the charger STAT pin",
                0.8,
            ),
            part(
                "LiPo battery connector",
                "battery",
                "S2B-PH-SM4-TB(LF)(SN)",
                "JST-PH is the common LiPo lead",
                0.85,
            ),
            part(
                "3.3 V LDO",
                "regulator",
                "AP2112K-3.3TRG1",
                "600 mA covers ESP32-C3 Wi-Fi TX peaks; 250 mV dropout",
                0.8,
                alts=["AP7361C-33E-13"],
            ),
            part(
                "main Wi-Fi MCU",
                "mcu",
                "ESP32-C3-WROOM-02-N4",
                "low-cost Wi-Fi/BLE module with native USB and deep sleep",
                0.85,
                alts=["ESP32-C6-MINI-1-N4"],
            ),
            part(
                "temperature/humidity sensor",
                "sensor",
                "SHT40-AD1B-R3",
                "+/-0.2 C, +/-1.8 %RH, I2C, 0.08 uA idle",
                0.9,
                alts=["SHTC3", "BME280"],
            ),
            part("status LED", "ui", "LTST-C191KGKT", "green status indicator", 0.8),
            part("user button", "ui", "PTS810 SJM 250 SMTR LFS", "small SMD tactile switch", 0.8),
        ],
        "notes": ["Support passives are added by the synthesizer."],
        "rejected_candidates": ["MCP1700T-3302E/TT: 250 mA cannot cover Wi-Fi TX peaks"],
    },
}

# ---------------------------------------------------------------------------
# 2. BLE coin-cell tag
# ---------------------------------------------------------------------------
EXAMPLES["ble_coin_tag"] = {
    "match": ["ble", "coin-cell", "cr2032", "accelerometer", "tag", "swd", "tag-connect"],
    "requirements": {
        "summary": "CR2032-powered BLE asset tag with wake-on-motion accelerometer.",
        "functional_goals": [
            "advertise over BLE",
            "wake on motion from an accelerometer interrupt",
            "status LED and one button",
            "SWD programming through a Tag-Connect footprint",
        ],
        "power_sources": [CR2032],
        "target_battery_life_days": 365,
        "duty_cycle": "BLE advertising once per second, about 3 ms radio-on per event",
        "duty_active_s": 0.003,
        "duty_period_s": 1.0,
        "interfaces": [
            {"kind": "I2C", "purpose": "accelerometer"},
            {"kind": "SWD", "purpose": "programming/debug", "external": True},
        ],
        "wireless": [{"protocol": "ble", "antenna": "any"}],
        "sensors": [{"quantity": "acceleration", "notes": "wake-on-motion interrupt"}],
        "user_io": ["status LED", "button"],
        "environment": {"temp_min_c": -10, "temp_max_c": 50},
        "size": {"max_width_mm": 40, "max_height_mm": 30},
        "layer_count_target": 2,
        "assumptions": [
            A("Advertising interval 1 s with ~3 ms active per event", "duty_active_s"),
            A(
                "Single-sided assembly with the coin holder on top: 40 x 30 mm. Moving the "
                "holder to the bottom side could shrink the board to about 30 x 25 mm.",
                "size",
            ),
            A("LED is used only briefly; excluded from the average current", "user_io"),
            A(
                "Runs directly from the coin cell, no regulator (nRF52 accepts 1.7-3.6 V)",
                "voltage_rails",
            ),
        ],
        "open_questions": ["Advertising payload and interval required by the asset system?"],
    },
    "architecture": {
        "blocks": [
            {"id": "battery", "kind": "battery", "name": "CR2032 holder", "covers": ["power"]},
            {"id": "mcu", "kind": "mcu_wireless", "name": "BLE module", "covers": ["ble"]},
            {"id": "sensor", "kind": "sensor", "name": "Accelerometer", "covers": ["acceleration"]},
            {
                "id": "ui",
                "kind": "user_io",
                "name": "LED and button",
                "covers": ["status LED", "button"],
            },
            {
                "id": "debug",
                "kind": "debug_programming",
                "name": "Tag-Connect SWD",
                "covers": ["SWD"],
            },
        ],
        "links": [
            {"src": "battery", "dst": "mcu", "kind": "power", "label": "VBAT 2.0-3.0 V"},
            {"src": "battery", "dst": "sensor", "kind": "power", "label": "VBAT"},
            {"src": "mcu", "dst": "sensor", "kind": "i2c", "label": "I2C + INT"},
            {"src": "mcu", "dst": "ui", "kind": "gpio"},
            {"src": "debug", "dst": "mcu", "kind": "swd"},
        ],
        "rationale": "Direct battery supply keeps quiescent current minimal.",
    },
    "components": {
        "parts": [
            part("CR2032 holder", "battery", "3034", "Keystone SMD coin-cell retainer", 0.9),
            part(
                "BLE MCU module",
                "mcu",
                "MDBT42Q-512KV2",
                "nRF52832 module with chip antenna, 2 uA sleep",
                0.85,
                alts=["E73-2G4M04S1B", "MDBT50Q-1MV2"],
            ),
            part(
                "wake-on-motion accelerometer",
                "sensor",
                "LIS3DHTR",
                "low-power accelerometer with interrupt",
                0.85,
                alts=["LSM6DSLTR"],
            ),
            part("status LED", "ui", "LTST-C191KGKT", "green indicator", 0.8),
            part("user button", "ui", "PTS810 SJM 250 SMTR LFS", "SMD tactile switch", 0.8),
            part(
                "SWD programming footprint",
                "debug",
                "TC2030-IDC-NL",
                "zero-cost Tag-Connect footprint",
                0.9,
                alts=["FTSH-105-01-L-DV-K"],
            ),
        ],
        "notes": [],
        "rejected_candidates": ["ESP32-C3-WROOM-02-N4: Wi-Fi peaks impossible on a coin cell"],
    },
}

# ---------------------------------------------------------------------------
# 3. 12 V motor + relay controller
# ---------------------------------------------------------------------------
EXAMPLES["relay_motor_12v"] = {
    "match": ["12", "relay", "motor", "screw", "terminals", "mounting", "reverse-polarity", "uart"],
    "requirements": {
        "summary": "12 V Wi-Fi controller for a small DC motor and a 5 V relay.",
        "functional_goals": [
            "drive one brushed DC motor up to 0.5 A in both directions",
            "switch one 5 V relay with contacts on a screw terminal",
            "protect the 12 V input against reverse polarity, overcurrent and surges",
            "UART programming header, status LED, four M3 mounting holes",
        ],
        "power_sources": [
            {
                "kind": "dc_jack",
                "nominal_v": 12.0,
                "min_v": 11.0,
                "max_v": 13.0,
                "max_current_ma": 1000,
                "notes": "12 V 1 A wall adapter, centre positive",
            }
        ],
        "voltage_rails": [
            {"name": "+5V", "nominal_v": 5.0, "purpose": "relay coil, LDO input"},
            {"name": "+3V3", "nominal_v": 3.3, "purpose": "MCU"},
        ],
        "interfaces": [
            {"kind": "UART", "purpose": "programming", "external": True},
            {"kind": "GPIO", "purpose": "motor and relay control"},
        ],
        "wireless": [{"protocol": "wifi", "antenna": "pcb"}],
        "actuators": [
            {"kind": "dc_motor", "voltage_v": 12, "current_ma": 500},
            {"kind": "relay", "voltage_v": 5, "current_ma": 72},
        ],
        "user_io": ["status LED"],
        "environment": {"temp_min_c": 0, "temp_max_c": 60},
        "size": {"max_width_mm": 80, "max_height_mm": 60},
        "layer_count_target": 2,
        "assumptions": [
            A("12 V 1 A centre-positive barrel adapter", "power_sources"),
            A("Relay contacts switch low-voltage loads only (<= 30 V DC)", "actuators"),
            A("Motor runs from the protected 12 V rail", "actuators"),
            A("Programming via external USB-UART adapter", "interfaces"),
        ],
        "open_questions": ["Motor stall current (sets the driver current limit)?"],
        "out_of_scope": ["switching mains voltage through the relay contacts"],
    },
    "architecture": {
        "blocks": [
            {
                "id": "power_in",
                "kind": "power_input",
                "name": "12 V input + protection",
                "covers": ["12 V", "protection"],
            },
            {"id": "buck", "kind": "regulation", "name": "12 V to 5 V buck", "covers": ["+5V"]},
            {"id": "ldo", "kind": "regulation", "name": "5 V to 3.3 V LDO", "covers": ["+3V3"]},
            {"id": "mcu", "kind": "mcu_wireless", "name": "Wi-Fi MCU", "covers": ["wifi"]},
            {
                "id": "motor",
                "kind": "actuator_driver",
                "name": "Motor H-bridge",
                "covers": ["dc_motor"],
            },
            {"id": "relay", "kind": "actuator_driver", "name": "Relay driver", "covers": ["relay"]},
            {"id": "debug", "kind": "debug_programming", "name": "UART header", "covers": ["UART"]},
            {"id": "ui", "kind": "user_io", "name": "Status LED", "covers": ["status LED"]},
            {"id": "mech", "kind": "other", "name": "Mounting holes", "covers": ["M3 holes"]},
        ],
        "links": [
            {"src": "power_in", "dst": "buck", "kind": "power", "label": "VIN 12 V"},
            {"src": "power_in", "dst": "motor", "kind": "power", "label": "VIN 12 V"},
            {"src": "buck", "dst": "ldo", "kind": "power", "label": "+5V"},
            {"src": "buck", "dst": "relay", "kind": "power", "label": "+5V"},
            {"src": "ldo", "dst": "mcu", "kind": "power", "label": "+3V3"},
            {"src": "mcu", "dst": "motor", "kind": "gpio", "label": "IN1/IN2"},
            {"src": "mcu", "dst": "relay", "kind": "gpio"},
            {"src": "debug", "dst": "mcu", "kind": "uart"},
            {"src": "mcu", "dst": "ui", "kind": "gpio"},
        ],
        "rationale": "Buck avoids dissipating 7 V across a linear regulator; LDO cleans the 3.3 V.",
    },
    "components": {
        "parts": [
            part("12 V barrel jack", "power_in", "PJ-063AH", "common 2.1 mm DC jack", 0.85),
            part(
                "input resettable fuse",
                "power_in",
                "MF-MSMF110-2",
                "1.1 A hold PTC above the ~0.75 A load",
                0.8,
            ),
            part(
                "reverse-polarity series diode",
                "power_in",
                "SS14",
                "1 A Schottky in series with VIN",
                0.75,
            ),
            part(
                "input surge TVS",
                "power_in",
                "SMAJ15A",
                "15 V standoff above the 13 V max input",
                0.8,
            ),
            part(
                "12 V to 5 V buck",
                "buck",
                "LM2596S-5.0/NOPB",
                "fixed 5 V, 3 A, simple asynchronous buck",
                0.75,
                alts=["TPS563200DDCR"],
            ),
            part("5 V to 3.3 V LDO", "ldo", "AP2112K-3.3TRG1", "600 mA LDO from 5 V", 0.8),
            part(
                "main Wi-Fi MCU",
                "mcu",
                "ESP32-WROOM-32E-N4",
                "Wi-Fi module with plenty of GPIO",
                0.8,
                alts=["ESP32-C3-WROOM-02-N4"],
            ),
            part(
                "DC motor H-bridge",
                "motor",
                "DRV8871DDAR",
                "3.6 A peak H-bridge with current limit, 6.5-45 V",
                0.8,
            ),
            part("motor output terminal", "motor", "1935161", "2-way 5.08 mm screw terminal", 0.85),
            part(
                "5 V relay",
                "relay",
                "SRD-05VDC-SL-C",
                "common SPDT 5 V relay",
                0.8,
                alts=["G5LE-1-DC12"],
            ),
            part("relay coil driver", "relay", "AO3400A", "logic-level N-MOSFET", 0.85),
            part("relay flyback diode", "relay", "1N4148W-7-F", "clamps coil turn-off spike", 0.85),
            part(
                "relay contacts terminal", "relay", "1935174", "3-way terminal for COM/NO/NC", 0.85
            ),
            part(
                "UART programming header",
                "debug",
                "61300411121",
                "4-pin 2.54 mm header for a USB-UART adapter",
                0.8,
            ),
            part("status LED", "ui", "LTST-C191KGKT", "green indicator", 0.8),
            part("M3 mounting hole", "mech", "MOUNTING-HOLE-M3", "four corner holes", 0.9, qty=4),
        ],
        "notes": [],
        "rejected_candidates": ["L7805CV: dissipates ~3 W from 12 V"],
    },
}

# ---------------------------------------------------------------------------
# 4. Intentionally flawed design
# ---------------------------------------------------------------------------
EXAMPLES["flawed_coin_wifi"] = {
    "match": ["logger", "10", "seconds", "redundancy", "identical", "15", "years"],
    "requirements": {
        "summary": "Wi-Fi T/RH logger on a CR2032 with a 5 V relay, two sensors, 15 x 15 mm.",
        "functional_goals": [
            "upload a temperature/humidity reading every 10 s over Wi-Fi",
            "run 2 years on one CR2032",
            "switch a 5 V relay",
            "two identical humidity sensors for redundancy",
        ],
        "power_sources": [CR2032],
        "target_battery_life_days": 730,
        "duty_cycle": "upload every 10 s, ~2 s awake",
        "duty_active_s": 2,
        "duty_period_s": 10,
        "voltage_rails": [{"name": "+3V3", "nominal_v": 3.3}],
        "interfaces": [{"kind": "I2C", "purpose": "two humidity sensors"}],
        "wireless": [{"protocol": "wifi", "antenna": "pcb"}],
        "sensors": [
            {"quantity": "humidity", "notes": "two identical parts"},
            {"quantity": "temperature"},
        ],
        "actuators": [{"kind": "relay", "voltage_v": 5, "current_ma": 72}],
        "size": {"max_width_mm": 15, "max_height_mm": 15},
        "assumptions": [A("Relay coil runs from the logic supply", "actuators")],
        "conflicts": [
            "A CR2032 cannot source Wi-Fi transmit peaks (300+ mA).",
            "2-year life with an upload every 10 s is not achievable on 220 mAh.",
            "A 15 x 15 mm board cannot hold a CR2032 holder plus a Wi-Fi module.",
        ],
        "open_questions": ["Is mains/USB power available instead of the coin cell?"],
    },
    "architecture": {
        "blocks": [
            {"id": "battery", "kind": "battery", "name": "CR2032 holder"},
            {"id": "regulator", "kind": "regulation", "name": "3.3 V LDO"},
            {"id": "mcu", "kind": "mcu_wireless", "name": "Wi-Fi MCU"},
            {"id": "sensors", "kind": "sensor", "name": "2x humidity sensor"},
            {"id": "relay", "kind": "actuator_driver", "name": "Relay"},
        ],
        "links": [
            {"src": "battery", "dst": "regulator", "kind": "power"},
            {"src": "regulator", "dst": "mcu", "kind": "power"},
            {"src": "regulator", "dst": "relay", "kind": "power"},
            {"src": "mcu", "dst": "sensors", "kind": "i2c"},
            {"src": "mcu", "dst": "relay", "kind": "gpio"},
        ],
        "rationale": "As requested, although several requirements conflict.",
    },
    "components": {
        "parts": [
            part("CR2032 holder", "battery", "3034", "requested coin cell", 0.6),
            part("3.3 V LDO", "regulator", "MCP1700T-3302E/TT", "1.6 uA quiescent current", 0.5),
            part("Wi-Fi MCU", "mcu", "ESP32-C3-WROOM-02-N4", "Wi-Fi requested", 0.6),
            part(
                "humidity sensor",
                "sensors",
                "SHT40-AD1B-R3",
                "two identical sensors as requested",
                0.7,
                qty=2,
            ),
            part("5 V relay", "relay", "SRD-05VDC-SL-C", "5 V relay requested", 0.6),
            part(
                "relay driver transistor",
                "relay",
                "MMBT3904",
                "generic NPN; not in the candidate list",
                0.3,
            ),
        ],
        "notes": ["Several requirements conflict; see requirements.conflicts."],
        "rejected_candidates": [],
    },
}

# ---------------------------------------------------------------------------
# Refinement fixture (example 1): reviewer asks for a pressure sensor instead
# ---------------------------------------------------------------------------
REFINE_FEEDBACK = "Replace the SHT40 with a BME280 so we also get barometric pressure."


def refine_components() -> dict[str, Any]:
    base = json.loads(json.dumps(EXAMPLES["iot_env_sensor"]["components"]))
    for p in base["parts"]:
        if p["mpn"] == "SHT40-AD1B-R3":
            p.update(
                role="temperature/humidity/pressure sensor",
                mpn="BME280",
                rationale="reviewer asked for barometric pressure; BME280 adds it on the same bus",
                confidence=0.85,
                alternates=["SHT40-AD1B-R3"],
            )
    base["notes"] = ["Revision: SHT40 replaced by BME280 per reviewer feedback."]
    return base


def write(stage: str, name: str, key: str, match: list[str], response: dict[str, Any]) -> None:
    out = ROOT / "fixtures" / "llm" / stage / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "_note": "Hand-authored fixture (not a model recording). See scripts/author_fixtures.py.",
        "match": match,
        "prompt_sha256": fixture_hash(key),
        "responses": [response],
    }
    out.write_text(json.dumps(doc, indent=2) + "\n")


def main() -> None:
    models = {
        "requirements": Requirements,
        "architecture": Architecture,
        "components": ComponentSelection,
    }
    for name, ex in EXAMPLES.items():
        prompt = (ROOT / "examples" / name / "prompt.txt").read_text().strip()
        for stage, model in models.items():
            model.model_validate(ex[stage])  # fail loudly if a fixture drifts from the schema
            write(stage, name, prompt, ex["match"], ex[stage])
            if name == "iot_env_sensor":
                write(stage, "_default", "", [], ex[stage])
    prompt = (ROOT / "examples" / "iot_env_sensor" / "prompt.txt").read_text().strip()
    key = fixture_key(prompt, [REFINE_FEEDBACK])
    comp = refine_components()
    ComponentSelection.model_validate(comp)
    write("components", "iot_env_sensor_refine_bme280", key, ["bme280", "barometric"], comp)
    print("fixtures written")


if __name__ == "__main__":
    main()
