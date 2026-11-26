"""Shared base model, enums and constants used by every schema."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

DISCLAIMER = (
    "AI-GENERATED DESIGN PLAN - NOT VERIFIED FOR FABRICATION. "
    "This output was produced with the help of a language model and deterministic rule checks. "
    "It must be reviewed and signed off by a qualified electronics engineer before any "
    "fabrication, assembly or use."
)

SCHEMA_VERSION = "1.0"


class Model(BaseModel):
    """Base for all schemas: strict about unknown keys so LLM drift is caught."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_assignment=True,
        use_enum_values=False,
    )


Confidence = Annotated[float, Field(ge=0.0, le=1.0, description="0 = guess, 1 = certain")]
RefDes = Annotated[
    str,
    Field(
        pattern=r"^[A-Z]{1,4}[0-9]{1,4}$",
        description="Reference designator, e.g. U1, C12, FB2, BT1",
        examples=["U1", "C3"],
    ),
]
# Spec values (key_specs) are deliberately scalar so they serialize to CSV/markdown cleanly.
SpecValue = str | int | float | bool


class PinType(StrEnum):
    POWER_IN = "power_in"
    POWER_OUT = "power_out"
    INPUT = "input"
    OUTPUT = "output"
    BIDIR = "bidir"
    PASSIVE = "passive"
    OPEN_DRAIN = "open_drain"
    NC = "nc"


class NetClassName(StrEnum):
    POWER = "power"
    GROUND = "ground"
    SIGNAL = "signal"
    RF = "rf"
    HIGH_SPEED = "high_speed"
    ANALOG = "analog"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class FindingCategory(StrEnum):
    ERC = "ERC"
    DFM = "DFM"
    DRC = "DRC"
    POWER = "power"
    PLACEMENT = "placement"
    LIBRARY = "library"
    REQUIREMENTS = "requirements"


class FindingSource(StrEnum):
    INTERNAL = "internal"
    KICAD = "kicad"


class ItemStatus(StrEnum):
    """Review state of an individual plan item (component, net, constraint...)."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


class ReviewStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class Side(StrEnum):
    TOP = "top"
    BOTTOM = "bottom"


class Edge(StrEnum):
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"


class Rect(Model):
    """Axis-aligned rectangle in board coordinates (mm, origin top-left, +y down)."""

    x_mm: float
    y_mm: float
    w_mm: float = Field(gt=0)
    h_mm: float = Field(gt=0)

    @property
    def x2(self) -> float:
        return self.x_mm + self.w_mm

    @property
    def y2(self) -> float:
        return self.y_mm + self.h_mm

    def intersects(self, other: Rect) -> bool:
        return not (
            self.x2 <= other.x_mm
            or other.x2 <= self.x_mm
            or self.y2 <= other.y_mm
            or other.y2 <= self.y_mm
        )

    def contains(self, other: Rect) -> bool:
        return (
            self.x_mm <= other.x_mm
            and self.y_mm <= other.y_mm
            and other.x2 <= self.x2
            and other.y2 <= self.y2
        )
