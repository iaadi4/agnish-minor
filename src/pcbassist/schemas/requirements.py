"""Structured requirements extracted from the natural-language prompt."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import Model

PowerSourceKind = Literal[
    "li_ion_1s",
    "lipo_1s",
    "coin_cell",
    "alkaline_aa",
    "alkaline_aaa",
    "usb_5v",
    "dc_jack",
    "dc_terminal",
    "poe",
    "energy_harvesting",
    "other",
]
InterfaceKind = Literal[
    "I2C",
    "SPI",
    "UART",
    "USB",
    "SWD",
    "JTAG",
    "GPIO",
    "ADC",
    "DAC",
    "PWM",
    "CAN",
    "RS485",
    "I2S",
    "ETHERNET",
    "OTHER",
]
WirelessProtocol = Literal[
    "wifi", "ble", "bluetooth_classic", "zigbee", "thread", "lora", "cellular", "nfc", "other"
]


class Assumption(Model):
    text: str = Field(description="The assumption, stated as a fact the design relies on")
    field: str | None = Field(default=None, description="Requirements field it fills, e.g. 'size'")
    rationale: str | None = None


class PowerSource(Model):
    kind: PowerSourceKind
    nominal_v: float
    min_v: float
    max_v: float
    capacity_mah: float | None = None
    max_current_ma: float | None = Field(default=None, description="What the source can deliver")
    rechargeable: bool = False
    notes: str | None = None


class RailRequirement(Model):
    name: str = Field(description="Net name, e.g. '+3V3', '+5V', 'VBAT', '+12V'")
    nominal_v: float
    tolerance_pct: float = 5.0
    est_current_ma: float | None = None
    purpose: str | None = None


class InterfaceRequirement(Model):
    kind: InterfaceKind
    purpose: str
    count: int = Field(default=1, ge=1)
    external: bool = Field(default=False, description="Exposed on a connector")


class WirelessRequirement(Model):
    protocol: WirelessProtocol
    notes: str | None = None
    antenna: Literal["pcb", "chip", "external", "any"] = "any"


class SensorRequirement(Model):
    quantity: str = Field(description="temperature, humidity, pressure, acceleration, light ...")
    accuracy: str | None = None
    notes: str | None = None


class ActuatorRequirement(Model):
    kind: Literal[
        "dc_motor", "stepper", "relay", "solenoid", "servo", "led_strip", "buzzer", "other"
    ]
    count: int = Field(default=1, ge=1)
    voltage_v: float | None = None
    current_ma: float | None = None
    notes: str | None = None


class EnvironmentalLimits(Model):
    temp_min_c: float = 0.0
    temp_max_c: float = 70.0
    ip_rating: str | None = None
    notes: str | None = None


class SizeLimits(Model):
    max_width_mm: float | None = None
    max_height_mm: float | None = None
    max_component_height_mm: float | None = None


class Requirements(Model):
    summary: str = Field(description="One-sentence restatement of what is being built")
    functional_goals: list[str] = Field(default_factory=list)
    power_sources: list[PowerSource] = Field(default_factory=list)
    target_battery_life_days: float | None = None
    duty_cycle: str | None = Field(
        default=None, description="Expected activity pattern, e.g. 'wake every 10 min, 2 s active'"
    )
    voltage_rails: list[RailRequirement] = Field(default_factory=list)
    interfaces: list[InterfaceRequirement] = Field(default_factory=list)
    wireless: list[WirelessRequirement] = Field(default_factory=list)
    sensors: list[SensorRequirement] = Field(default_factory=list)
    actuators: list[ActuatorRequirement] = Field(default_factory=list)
    user_io: list[str] = Field(default_factory=list, description="Buttons, LEDs, displays ...")
    environment: EnvironmentalLimits = Field(default_factory=EnvironmentalLimits)
    size: SizeLimits = Field(default_factory=SizeLimits)
    layer_count_target: int | None = Field(default=None, ge=1, le=12)
    cost_target: str | None = None
    assumptions: list[Assumption] = Field(
        default_factory=list, description="Everything filled in that the prompt did not state"
    )
    open_questions: list[str] = Field(
        default_factory=list, description="Questions a human must answer before sign-off"
    )
    conflicts: list[str] = Field(
        default_factory=list,
        description="Contradictory or infeasible requirements found in the prompt",
    )
    out_of_scope: list[str] = Field(
        default_factory=list, description="Requested items this tool cannot design (e.g. mains PSU)"
    )
