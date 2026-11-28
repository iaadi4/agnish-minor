"""What the LLM authors in the parts and netlist stages.

The model picks real parts (exact KiCad symbol and footprint ids, or a curated-library
MPN) and writes the pin-level netlist as ``REF.PIN`` endpoints. It never writes
coordinates. Every field is checked by deterministic code (``pipeline/intent.py``)
against the KiCad library index and the electrical rule checks; findings go back to the
model until the design validates.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import Model, NetClassName, RefDes


class PartRatings(Model):
    """Datasheet figures the model states for a part that is not in the curated library.

    They are the model's claims, not verified data: the plan marks every such part
    UNVERIFIED and the report lists them for the reviewer to check against the datasheet.
    """

    supply_min_v: float | None = None
    supply_max_v: float | None = None
    typ_ma: float | None = Field(default=None, ge=0, description="Typical active current")
    max_ma: float | None = Field(default=None, ge=0, description="Peak current drawn")
    sleep_ua: float | None = Field(default=None, ge=0, description="Sleep / quiescent current")
    output_max_ma: float | None = Field(
        default=None, ge=0, description="Regulators/sources: rated output current"
    )
    voltage_rating_v: float | None = Field(
        default=None, gt=0, description="Capacitors, diodes, TVS: voltage rating"
    )


class PartEntry(Model):
    ref: RefDes
    value: str = Field(
        min_length=1,
        max_length=40,
        description="Component value as drawn on the schematic ('100nF', '10k', 'ESP32-C3'); "
        "never prose",
    )
    block_id: str = Field(description="Architecture block id this part belongs to")
    role: str = Field(description="What it does here, e.g. '3V3 LDO', 'U1 VDD decoupling'")
    mpn: str | None = Field(
        default=None,
        description="Manufacturer part number. Required for active parts. Use a curated-"
        "library MPN when one fits: its ratings are verified.",
    )
    manufacturer: str | None = None
    lib_id: str | None = Field(
        default=None,
        description="Exact KiCad symbol 'Lib:Name'. Derived for curated MPNs and for passives "
        "given 'package'.",
    )
    footprint: str | None = Field(
        default=None, description="Exact KiCad footprint 'Lib:Name' (derived like lib_id)"
    )
    package: str | None = Field(
        default=None,
        description="Passive shorthand instead of lib_id/footprint: '0402', '0603', '0805', "
        "'1206', 'SOD-123', 'SMA', '3225' ...",
    )
    support_for: RefDes | None = Field(
        default=None, description="Ref of the part this one serves (decoupling, pull-up ...)"
    )
    rationale: str = ""
    ratings: PartRatings | None = Field(
        default=None,
        description="Datasheet figures for parts outside the curated library (unverified); "
        "for curated parts only the design-specific load currents (typ/max/sleep) are used",
    )
    off_board: bool = Field(
        default=False, description="A BOM line with no place on the board (cable, cell)"
    )
    no_part: bool = Field(
        default=False, description="A board feature with nothing to buy (mounting hole)"
    )


class Substitution(Model):
    ref: RefDes
    wanted: str
    chosen: str
    reason: str = Field(min_length=3)


class Blocker(Model):
    """A requirement no available part can meet: the design stops for a human decision."""

    constraint: str = Field(description="The requirement, e.g. 'battery_life >= 1 year'")
    required: str
    found: str = Field(description="Best candidate and why it fails, with the arithmetic")


class PartsManifest(Model):
    parts: list[PartEntry] = Field(min_length=1)
    substitutions: list[Substitution] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class IntentNet(Model):
    name: str = Field(pattern=r"^[A-Za-z0-9_+\-./~]+$", max_length=40)
    pins: list[str] = Field(description='Endpoints "REF.PIN" using real pin numbers')
    kind: Literal["power", "ground", "signal"] | None = Field(
        default=None, description="Override; otherwise inferred from pin types"
    )
    net_class: NetClassName | None = Field(
        default=None, description="rf / high_speed / analog when it matters for routing"
    )
    voltage_v: float | None = Field(default=None, description="Nominal voltage of a power net")
    notes: str | None = None


class IntentNoConnect(Model):
    pin: str = Field(description='"REF.PIN"')
    reason: str = Field(min_length=3)


class RailIntent(Model):
    net: str = Field(description="Power net name, e.g. '+3V3'")
    voltage_v: float
    source_ref: RefDes | None = Field(default=None, description="Regulator/connector driving it")
    source_kind: Literal[
        "usb", "dc_input", "battery", "ldo", "buck", "boost", "charger", "load_switch", "other"
    ] = "other"
    parent_net: str | None = Field(default=None, description="Rail feeding the source")
    max_current_ma: float | None = Field(
        default=None, ge=0, description="What the source can deliver (battery, USB port ...)"
    )
    min_v: float | None = None
    max_v: float | None = None


class NetlistIntent(Model):
    nets: list[IntentNet] = Field(min_length=1)
    no_connect: list[IntentNoConnect] = Field(default_factory=list)
    rails: list[RailIntent] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
