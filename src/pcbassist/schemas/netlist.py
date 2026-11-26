"""Pin-level netlist."""

from __future__ import annotations

from pydantic import Field, model_validator

from .common import ItemStatus, Model, NetClassName, RefDes


class NetConnection(Model):
    ref: RefDes
    pin: str = Field(description="Pad number on the component")


class Net(Model):
    name: str = Field(pattern=r"^[A-Za-z0-9_+\-./~]+$")
    net_class: NetClassName = NetClassName.SIGNAL
    connections: list[NetConnection] = Field(default_factory=list)
    voltage_v: float | None = Field(default=None, description="Nominal voltage for power nets")
    notes: str | None = None
    status: ItemStatus = ItemStatus.PROPOSED
    locked: bool = False


class NoConnect(Model):
    ref: RefDes
    pin: str
    reason: str = Field(min_length=3)


class Netlist(Model):
    nets: list[Net] = Field(default_factory=list)
    no_connects: list[NoConnect] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> Netlist:
        names = [n.name for n in self.nets]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate net names: {dupes}")
        return self

    def net(self, name: str) -> Net | None:
        return next((n for n in self.nets if n.name == name), None)

    def nets_of(self, ref: str, pin: str) -> list[Net]:
        return [n for n in self.nets if any(c.ref == ref and c.pin == pin for c in n.connections)]
