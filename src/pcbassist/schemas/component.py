"""Component, pin and library-part schemas."""

from __future__ import annotations

from pydantic import Field, model_validator

from .common import Confidence, ItemStatus, Model, PinType, RefDes, SpecValue


class Pin(Model):
    number: str = Field(description="Pad number as in the KiCad footprint, e.g. '1', 'A4', 'EP'")
    name: str = Field(description="Pin name from the symbol, e.g. 'VDD', 'IO9', 'SDA'")
    type: PinType
    functions: list[str] = Field(
        default_factory=list,
        description="Function tags: gnd, power, gpio, i2c_sda, usb_dp, strapping, reset ...",
    )


class SupplyRange(Model):
    min_v: float
    max_v: float

    @model_validator(mode="after")
    def _ordered(self) -> SupplyRange:
        if self.min_v > self.max_v:
            raise ValueError(f"supply min_v {self.min_v} > max_v {self.max_v}")
        return self

    def contains(self, v: float) -> bool:
        return self.min_v <= v <= self.max_v


class CurrentProfile(Model):
    typ_ma: float | None = Field(default=None, ge=0, description="Typical active current")
    max_ma: float | None = Field(
        default=None, ge=0, description="Peak current the rail must supply"
    )
    sleep_ua: float | None = Field(default=None, ge=0, description="Lowest sleep/retention current")


class FootprintInfo(Model):
    """Geometry summary of a KiCad footprint, used for placement and DFM."""

    name: str = Field(description="KiCad 'Library:Footprint' name")
    pad_numbers: list[str] = Field(default_factory=list)
    courtyard_w_mm: float | None = None
    courtyard_h_mm: float | None = None
    smd: bool = True
    min_pad_pitch_mm: float | None = Field(
        default=None, description="Smallest centre-to-centre distance between distinct pads"
    )


class LibraryPart(Model):
    """A curated part from the component library (seed data)."""

    mpn: str
    manufacturer: str
    category: str
    description: str
    package: str | None = None
    footprint: str
    footprint_info: FootprintInfo | None = None
    ref_prefix: str = Field(pattern=r"^[A-Z]{1,4}$")
    value: str | None = None
    pins: list[Pin]
    supply_range: SupplyRange | None = None
    current: CurrentProfile = Field(default_factory=CurrentProfile)
    key_specs: dict[str, SpecValue] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    datasheet_url: str | None = Field(
        default=None,
        description="Only from a trusted source (KiCad symbol metadata); never invented",
    )
    kicad_symbol: str | None = None
    pin_source: str = Field(
        description="Where the pin table came from, e.g. 'kicad-symbols@master'"
    )
    in_bom: bool = True
    data_confidence: str = Field(default="medium", pattern=r"^(high|medium|low)$")
    notes: str | None = None


class Component(Model):
    """A component instance placed in a design."""

    ref_des: RefDes
    role: str = Field(description="What it does in this design, e.g. 'main MCU', 'I2C pull-up SDA'")
    mpn: str
    category: str | None = Field(default=None, description="Library category, e.g. 'ldo'")
    manufacturer: str | None = None
    description: str = ""
    package: str | None = None
    footprint: str | None = Field(default=None, description="KiCad 'Library:Footprint'")
    value: str | None = None
    key_specs: dict[str, SpecValue] = Field(default_factory=dict)
    pins: list[Pin] = Field(default_factory=list)
    supply_range: SupplyRange | None = None
    current: CurrentProfile = Field(default_factory=CurrentProfile)
    alternates: list[str] = Field(default_factory=list)
    datasheet_url: str | None = None
    kicad_symbol: str | None = Field(default=None, description="KiCad 'Lib:Symbol' of the part")
    rationale: str = ""
    confidence: Confidence = 0.5
    verified: bool = Field(
        default=False,
        description="True only if the part (pins and ratings) came from the curated library",
    )
    pins_verified: bool = Field(
        default=False,
        description="True if the pin table came from a KiCad symbol (ratings may be unverified)",
    )
    data_source: str | None = Field(
        default=None, description="library | kicad | synth: where pins and ratings came from"
    )
    block_id: str | None = Field(
        default=None, description="Architecture block this part belongs to"
    )
    rail: str | None = Field(default=None, description="Supply rail name the part is powered from")
    support_for: str | None = Field(
        default=None, description="ref_des of the part this support component serves"
    )
    support_pin: str | None = Field(
        default=None, description="Pin of `support_for` it serves (decoupling, pull-up ...)"
    )
    key: str | None = Field(
        default=None, description="Stable synthesis key, used to keep ref_des across revisions"
    )
    origin: str = Field(
        default="llm",
        pattern=r"^(llm|synth|human)$",
        description="Who added it: LLM selection, deterministic synthesizer, or a reviewer",
    )
    in_bom: bool = True
    status: ItemStatus = ItemStatus.PROPOSED
    locked: bool = False

    def pin(self, number: str) -> Pin | None:
        return next((p for p in self.pins if p.number == number), None)

    @property
    def pins_known(self) -> bool:
        """The pin table is real (curated library or KiCad symbol), so ERC can check it."""
        return self.verified or self.pins_verified

    def pins_named(self, name: str) -> list[Pin]:
        return [p for p in self.pins if p.name == name]
