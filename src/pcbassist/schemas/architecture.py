"""Block-level architecture."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from .common import Model

BlockKind = Literal[
    "power_input",
    "charging",
    "battery",
    "regulation",
    "mcu_wireless",
    "mcu",
    "sensor",
    "actuator_driver",
    "connector",
    "protection",
    "debug_programming",
    "user_io",
    "clock",
    "isolation",
    "other",
]
LinkKind = Literal[
    "power", "ground", "i2c", "spi", "uart", "usb", "gpio", "analog", "rf", "swd", "other"
]


class Block(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="snake_case id, e.g. 'charger'")
    kind: BlockKind
    name: str
    description: str = ""
    covers: list[str] = Field(
        default_factory=list, description="Requirement items this block satisfies"
    )


class BlockLink(Model):
    src: str
    dst: str
    kind: LinkKind
    label: str = ""


class Architecture(Model):
    blocks: list[Block]
    links: list[BlockLink] = Field(default_factory=list)
    rationale: str = ""

    @model_validator(mode="after")
    def _links_reference_blocks(self) -> Architecture:
        ids = [b.id for b in self.blocks]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate block ids: {sorted(dupes)}")
        known = set(ids)
        bad = [
            f"{lk.src}->{lk.dst}" for lk in self.links if lk.src not in known or lk.dst not in known
        ]
        if bad:
            raise ValueError(f"links reference unknown blocks: {bad}")
        return self

    def block(self, block_id: str) -> Block | None:
        return next((b for b in self.blocks if b.id == block_id), None)

    def to_mermaid(self) -> str:
        """Render as a Mermaid flowchart (left-to-right)."""

        def esc(s: str) -> str:
            return re.sub(r'["\[\]{}()<>|]', " ", s).strip()

        lines = ["flowchart LR"]
        for b in self.blocks:
            lines.append(f'    {b.id}["{esc(b.name)}<br/><i>{esc(b.kind)}</i>"]')
        for lk in self.links:
            label = esc(lk.label or lk.kind)
            arrow = "==>" if lk.kind == "power" else "-->"
            lines.append(f"    {lk.src} {arrow}|{label}| {lk.dst}")
        return "\n".join(lines)
