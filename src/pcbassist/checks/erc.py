"""Electrical rule checks on the pin-level netlist.

Each rule is a small function ``(ctx) -> list[Finding]`` so it can be tested on its own by
mutating a netlist. Rule ids are stable; finding ids are ``<rule>:<subject>``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from ..schemas import (
    Component,
    DesignPlan,
    Finding,
    FindingCategory,
    Net,
    NetClassName,
    Pin,
    PinType,
    Severity,
)

DRIVERS = {PinType.OUTPUT, PinType.POWER_OUT}
IC_CATEGORIES = {
    "mcu_wireless",
    "mcu",
    "charger",
    "fuel_gauge",
    "ldo",
    "buck",
    "boost",
    "sensor_env",
    "sensor_motion",
    "sensor_temp",
    "sensor_light",
    "motor_driver",
    "transistor_array",
    "usb_uart",
    "load_switch",
    "rtc",
}


@dataclass
class Ctx:
    plan: DesignPlan
    comps: dict[str, Component] = field(default_factory=dict)
    nets: dict[str, Net] = field(default_factory=dict)
    pin_nets: dict[tuple[str, str], list[str]] = field(default_factory=lambda: defaultdict(list))
    nc: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def build(cls, plan: DesignPlan) -> Ctx:
        ctx = cls(plan=plan)
        ctx.comps = {c.ref_des: c for c in plan.components}
        if plan.netlist is not None:
            ctx.nets = {n.name: n for n in plan.netlist.nets}
            for n in plan.netlist.nets:
                for c in n.connections:
                    ctx.pin_nets[(c.ref, c.pin)].append(n.name)
            ctx.nc = {(x.ref, x.pin): x.reason for x in plan.netlist.no_connects}
        return ctx

    def pin(self, ref: str, number: str) -> Pin | None:
        c = self.comps.get(ref)
        return c.pin(number) if c else None

    def members(self, net: str) -> list[tuple[Component, Pin | None]]:
        n = self.nets.get(net)
        if n is None:
            return []
        out = []
        for c in n.connections:
            comp = self.comps.get(c.ref)
            if comp is not None:
                out.append((comp, comp.pin(c.pin)))
        return out

    def net_of(self, ref: str, pin: str) -> str | None:
        nets = self.pin_nets.get((ref, pin))
        return nets[0] if nets else None

    def is_ground(self, net: str) -> bool:
        n = self.nets.get(net)
        return n is not None and (n.net_class == NetClassName.GROUND or net.upper() == "GND")

    def voltage(self, net: str) -> float | None:
        n = self.nets.get(net)
        if n is not None and n.voltage_v is not None:
            return n.voltage_v
        for r in self.plan.power_tree:
            if r.name == net:
                return r.nominal_v
        return None

    def rail_range(self, net: str) -> tuple[float, float] | None:
        rails = (
            self.plan.constraints.electrical.rails
            if self.plan.constraints is not None
            else self.plan.power_tree
        )
        for r in rails or self.plan.power_tree:
            if r.name == net:
                return (r.min_v, r.max_v)
        v = self.voltage(net)
        return (v, v) if v is not None else None


def F(
    rule: str,
    subject: str,
    sev: Severity,
    msg: str,
    *,
    refs: list[str] | None = None,
    nets: list[str] | None = None,
    fix: str | None = None,
) -> Finding:
    return Finding(
        id=f"{rule}:{subject}",
        rule=rule,
        severity=sev,
        category=FindingCategory.ERC,
        message=msg,
        refs=refs or [],
        nets=nets or [],
        suggested_fix=fix,
    )


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def rule_unconnected(ctx: Ctx) -> list[Finding]:
    """Every pin of a verified part must be on a net or be an explicit no-connect."""
    out = []
    for c in ctx.plan.components:
        if not c.verified:
            continue
        missing = [
            p
            for p in c.pins
            if (c.ref_des, p.number) not in ctx.pin_nets and (c.ref_des, p.number) not in ctx.nc
        ]
        for p in missing:
            power = p.type in (PinType.POWER_IN, PinType.POWER_OUT) or "gnd" in p.functions
            out.append(
                F(
                    "ERC-UNCONNECTED",
                    f"{c.ref_des}.{p.number}",
                    Severity.ERROR if power else Severity.WARNING,
                    f"{c.ref_des} pin {p.number} ({p.name or '~'}) is neither connected nor "
                    "marked no-connect.",
                    refs=[c.ref_des],
                    fix="Connect it or add an explicit no-connect with a reason.",
                )
            )
    return out


def rule_nc_conflicts(ctx: Ctx) -> list[Finding]:
    out = []
    for (ref, pin), nets in ctx.pin_nets.items():
        if len(nets) > 1:
            out.append(
                F(
                    "ERC-MULTI-NET",
                    f"{ref}.{pin}",
                    Severity.ERROR,
                    f"{ref} pin {pin} is on several nets ({', '.join(nets)}): they are shorted.",
                    refs=[ref],
                    nets=nets,
                )
            )
        if (ref, pin) in ctx.nc:
            out.append(
                F(
                    "ERC-NC-CONNECTED",
                    f"{ref}.{pin}",
                    Severity.ERROR,
                    f"{ref} pin {pin} is marked no-connect but is on net {nets[0]}.",
                    refs=[ref],
                    nets=nets,
                )
            )
        p = ctx.pin(ref, pin)
        if p is not None and p.type == PinType.NC:
            out.append(
                F(
                    "ERC-NC-PIN-USED",
                    f"{ref}.{pin}",
                    Severity.WARNING,
                    f"{ref} pin {pin} is an NC pin per its symbol but is connected to {nets[0]}.",
                    refs=[ref],
                    nets=nets,
                )
            )
        if ctx.comps.get(ref) is None:
            out.append(
                F(
                    "ERC-UNKNOWN-REF",
                    f"{ref}.{pin}",
                    Severity.ERROR,
                    f"Net {nets[0]} references {ref}, which is not in the component list.",
                    nets=nets,
                )
            )
        elif p is None:
            out.append(
                F(
                    "ERC-UNKNOWN-PIN",
                    f"{ref}.{pin}",
                    Severity.ERROR,
                    f"{ref} has no pin {pin} (net {nets[0]}).",
                    refs=[ref],
                    nets=nets,
                )
            )
    return out


def rule_single_pin_nets(ctx: Ctx) -> list[Finding]:
    out = []
    for n in ctx.nets.values():
        if len(n.connections) < 2:
            out.append(
                F(
                    "ERC-SINGLE-PIN-NET",
                    n.name,
                    Severity.WARNING,
                    f"Net {n.name} has only {len(n.connections)} connection(s).",
                    refs=[c.ref for c in n.connections],
                    nets=[n.name],
                    fix="Connect it to its destination (e.g. a terminal) or remove it.",
                )
            )
    return out


def rule_driver_conflicts(ctx: Ctx) -> list[Finding]:
    out = []
    for n in ctx.nets.values():
        drivers = [
            (comp.ref_des, p)
            for comp, p in ctx.members(n.name)
            if p is not None and p.type in DRIVERS
        ]
        if len(drivers) > 1 and not ctx.is_ground(n.name):
            kinds = {p.type for _, p in drivers}
            out.append(
                F(
                    "ERC-DRIVER-CONFLICT",
                    n.name,
                    Severity.ERROR,
                    f"Net {n.name} has {len(drivers)} drivers "
                    f"({', '.join(f'{r}.{p.number}' for r, p in drivers)}; "
                    f"{'/'.join(sorted(k.value for k in kinds))}).",
                    refs=[r for r, _ in drivers],
                    nets=[n.name],
                    fix="Only one output may drive a net; insert an OR-ing element or split it.",
                )
            )
    return out


def rule_power_undriven(ctx: Ctx) -> list[Finding]:
    out = []
    for n in ctx.nets.values():
        if ctx.is_ground(n.name):
            continue
        members = ctx.members(n.name)
        sinks = [(c, p) for c, p in members if p is not None and p.type == PinType.POWER_IN]
        if not sinks:
            continue
        driven = any(
            p is not None and (p.type in DRIVERS or p.type == PinType.PASSIVE) for _, p in members
        )
        if not driven:
            out.append(
                F(
                    "ERC-POWER-UNDRIVEN",
                    n.name,
                    Severity.ERROR,
                    f"Net {n.name} feeds power inputs "
                    f"({', '.join(c.ref_des for c, _ in sinks)}) but nothing drives it.",
                    refs=[c.ref_des for c, _ in sinks],
                    nets=[n.name],
                )
            )
    return out


def rule_ground_pins(ctx: Ctx) -> list[Finding]:
    out = []
    for c in ctx.plan.components:
        if not c.verified:
            continue
        for p in c.pins:
            if "gnd" not in p.functions:
                continue
            net = ctx.net_of(c.ref_des, p.number)
            if net is not None and not ctx.is_ground(net):
                out.append(
                    F(
                        "ERC-GROUND-PIN",
                        f"{c.ref_des}.{p.number}",
                        Severity.ERROR,
                        f"{c.ref_des} ground pin {p.number} ({p.name}) is on {net}, not GND.",
                        refs=[c.ref_des],
                        nets=[net],
                    )
                )
    return out


def rule_supply_range(ctx: Ctx) -> list[Finding]:
    out = []
    for c in ctx.plan.components:
        if not c.verified or c.supply_range is None or c.category in ("esd",):
            continue
        nets = {
            n
            for p in c.pins
            if p.type == PinType.POWER_IN and "gnd" not in p.functions
            for n in ctx.pin_nets.get((c.ref_des, p.number), [])
        }
        if not nets and c.rail:
            nets = {c.rail}
        for net in sorted(nets):
            rng = ctx.rail_range(net)
            if rng is None:
                continue
            lo, hi = rng
            sr = c.supply_range
            nominal = ctx.voltage(net)
            if nominal is not None and not sr.contains(nominal):
                out.append(
                    F(
                        "ERC-SUPPLY-RANGE",
                        f"{c.ref_des}:{net}",
                        Severity.ERROR,
                        f"{c.ref_des} ({c.mpn}) runs from {net} = {nominal:g} V but needs "
                        f"{sr.min_v:g}-{sr.max_v:g} V.",
                        refs=[c.ref_des],
                        nets=[net],
                        fix="Power it from a rail inside its supply range.",
                    )
                )
            elif hi > sr.max_v + 1e-9:
                out.append(
                    F(
                        "ERC-SUPPLY-RANGE",
                        f"{c.ref_des}:{net}",
                        Severity.ERROR,
                        f"{net} can reach {hi:g} V, above {c.ref_des} maximum {sr.max_v:g} V.",
                        refs=[c.ref_des],
                        nets=[net],
                    )
                )
            elif lo < sr.min_v - 1e-9:
                out.append(
                    F(
                        "ERC-SUPPLY-RANGE",
                        f"{c.ref_des}:{net}",
                        Severity.WARNING,
                        f"{net} can fall to {lo:g} V, below {c.ref_des} minimum {sr.min_v:g} V "
                        "(brown-out at the end of the source's range).",
                        refs=[c.ref_des],
                        nets=[net],
                    )
                )
    return out


def _caps_between(ctx: Ctx, net: str) -> list[Component]:
    caps = []
    for comp, _ in ctx.members(net):
        if comp.category != "capacitor":
            continue
        other = [ctx.net_of(comp.ref_des, p.number) for p in comp.pins]
        if any(o is not None and ctx.is_ground(o) for o in other):
            caps.append(comp)
    return caps


def rule_decoupling(ctx: Ctx) -> list[Finding]:
    out = []
    for c in ctx.plan.components:
        if not c.verified or c.category not in IC_CATEGORIES:
            continue
        for p in c.pins:
            if p.type != PinType.POWER_IN or "gnd" in p.functions:
                continue
            net = ctx.net_of(c.ref_des, p.number)
            if net is None or ctx.is_ground(net):
                continue
            caps = _caps_between(ctx, net)
            own = [k for k in caps if k.support_for == c.ref_des]
            if not caps:
                out.append(
                    F(
                        "ERC-DECOUPLING",
                        f"{c.ref_des}.{p.number}",
                        Severity.ERROR,
                        f"{c.ref_des} supply pin {p.number} ({p.name}) on {net} has no "
                        "capacitor to GND anywhere on that net.",
                        refs=[c.ref_des],
                        nets=[net],
                        fix="Add a 100 nF ceramic capacitor next to the pin.",
                    )
                )
            elif not own:
                out.append(
                    F(
                        "ERC-DECOUPLING",
                        f"{c.ref_des}.{p.number}",
                        Severity.WARNING,
                        f"{c.ref_des} pin {p.number} ({p.name}) relies on capacitors placed for "
                        f"other parts ({', '.join(k.ref_des for k in caps[:3])}).",
                        refs=[c.ref_des],
                        nets=[net],
                        fix="Give the pin its own local decoupling capacitor.",
                    )
                )
    return out


def _bus_nets(ctx: Ctx, func: str) -> list[str]:
    out = set()
    for c in ctx.plan.components:
        for p in c.pins:
            if func in p.functions and c.category not in ("connector_header",):
                n = ctx.net_of(c.ref_des, p.number)
                if n is not None:
                    out.add(n)
    return sorted(out)


def rule_i2c(ctx: Ctx) -> list[Finding]:
    out = []
    for net in _bus_nets(ctx, "i2c_sda") + _bus_nets(ctx, "i2c_scl"):
        pullups = []
        for comp, _ in ctx.members(net):
            if comp.category != "resistor":
                continue
            others = [ctx.net_of(comp.ref_des, p.number) for p in comp.pins]
            if any(o is not None and o != net and ctx.voltage(o) for o in others):
                pullups.append(comp.ref_des)
        if not pullups:
            out.append(
                F(
                    "ERC-I2C-PULLUP",
                    net,
                    Severity.ERROR,
                    f"I2C net {net} has no pull-up resistor to a supply.",
                    nets=[net],
                    fix="Add a 2.2-10 kOhm pull-up to the bus supply.",
                )
            )
        elif len(pullups) > 1:
            out.append(
                F(
                    "ERC-I2C-PULLUP",
                    net,
                    Severity.INFO,
                    f"I2C net {net} has {len(pullups)} pull-ups in parallel "
                    f"({', '.join(pullups)}).",
                    nets=[net],
                )
            )
    by_bus: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for c in ctx.plan.components:
        addr = c.key_specs.get("i2c_address_assigned")
        sda = next((p for p in c.pins if "i2c_sda" in p.functions), None)
        if not isinstance(addr, str) or sda is None:
            continue
        bus = ctx.net_of(c.ref_des, sda.number)
        if bus:
            by_bus[bus].append((addr, c.ref_des))
    for bus, items in by_bus.items():
        seen: dict[str, list[str]] = defaultdict(list)
        for addr, ref in items:
            seen[addr].append(ref)
        for addr, refs in seen.items():
            if len(refs) > 1:
                out.append(
                    F(
                        "ERC-I2C-ADDRESS",
                        f"{bus}:{addr}",
                        Severity.ERROR,
                        f"{', '.join(refs)} share I2C address {addr} on {bus}.",
                        refs=refs,
                        nets=[bus],
                        fix="Use parts with selectable addresses, an I2C mux, or a second bus.",
                    )
                )
    return out


def rule_usb(ctx: Ctx) -> list[Finding]:
    out = []
    for c in ctx.plan.components:
        if c.category != "connector_usb" or not c.verified:
            continue
        data = [
            ctx.net_of(c.ref_des, p.number)
            for p in c.pins
            if {"usb_dp", "usb_dm"} & set(p.functions)
        ]
        data_nets = sorted({n for n in data if n})
        if data_nets:
            esd = any(comp.category == "esd" for n in data_nets for comp, _ in ctx.members(n))
            if not esd:
                out.append(
                    F(
                        "ERC-USB-ESD",
                        c.ref_des,
                        Severity.WARNING,
                        f"USB data lines on {c.ref_des} have no ESD protection.",
                        refs=[c.ref_des],
                        nets=data_nets,
                        fix="Add a low-capacitance TVS array (e.g. USBLC6-2SC6) at the connector.",
                    )
                )
        for p in c.pins:
            if "usb_cc" not in p.functions:
                continue
            net = ctx.net_of(c.ref_des, p.number)
            ok = False
            if net:
                for comp, _ in ctx.members(net):
                    if comp.category == "resistor":
                        others = [ctx.net_of(comp.ref_des, q.number) for q in comp.pins]
                        if any(o and ctx.is_ground(o) for o in others):
                            ok = comp.value in ("5.1k", "5k1")
            if not ok:
                out.append(
                    F(
                        "ERC-USB-CC",
                        f"{c.ref_des}.{p.number}",
                        Severity.ERROR,
                        f"{c.ref_des} {p.name} lacks a 5.1 kOhm pull-down: a USB-C source will "
                        "not supply VBUS.",
                        refs=[c.ref_des],
                        nets=[net] if net else [],
                    )
                )
    return out


def rule_floating_inputs(ctx: Ctx) -> list[Finding]:
    out = []
    for n in ctx.nets.values():
        members = ctx.members(n.name)
        if not members or ctx.voltage(n.name) is not None:
            continue
        types = {p.type for _, p in members if p is not None}
        if types and types <= {PinType.INPUT}:
            out.append(
                F(
                    "ERC-FLOATING-INPUT",
                    n.name,
                    Severity.WARNING,
                    f"Net {n.name} connects only inputs; nothing sets its level.",
                    refs=[c.ref_des for c, _ in members],
                    nets=[n.name],
                )
            )
    return out


def rule_inductive_loads(ctx: Ctx) -> list[Finding]:
    out = []
    for c in ctx.plan.components:
        if c.category != "relay" or not c.verified:
            continue
        coil_nets = set()
        for p in c.pins:
            n = ctx.net_of(c.ref_des, p.number)
            if n and (ctx.rail_range(n) or "COIL" in n):
                coil_nets.add(n)
        clamped = False
        for d in ctx.plan.components:
            if d.category != "diode" or not d.verified:
                continue
            dn = {ctx.net_of(d.ref_des, p.number) for p in d.pins}
            if len(coil_nets & dn) == 2:
                clamped = True
        if not clamped:
            out.append(
                F(
                    "ERC-FLYBACK",
                    c.ref_des,
                    Severity.ERROR,
                    f"Relay {c.ref_des} coil has no flyback diode across it.",
                    refs=[c.ref_des],
                    fix="Add a diode across the coil (cathode to the supply side).",
                )
            )
    return out


def rule_led_resistor(ctx: Ctx) -> list[Finding]:
    out = []
    for c in ctx.plan.components:
        if c.category != "led" or not c.verified:
            continue
        nets = [n for p in c.pins if (n := ctx.net_of(c.ref_des, p.number))]
        # the series resistor must sit on the LED's own (non-rail, non-ground) net
        local = [n for n in nets if not ctx.is_ground(n) and ctx.rail_range(n) is None]
        has_r = any(comp.category == "resistor" for n in local for comp, _ in ctx.members(n))
        if nets and not has_r:
            out.append(
                F(
                    "ERC-LED-RESISTOR",
                    c.ref_des,
                    Severity.ERROR,
                    f"LED {c.ref_des} has no series resistor; its current is unlimited.",
                    refs=[c.ref_des],
                )
            )
    return out


def rule_cap_voltage(ctx: Ctx) -> list[Finding]:
    out = []
    for c in ctx.plan.components:
        if c.category != "capacitor":
            continue
        rating = c.key_specs.get("voltage_v")
        if not isinstance(rating, (int, float)):
            continue
        vmax = 0.0
        for p in c.pins:
            n = ctx.net_of(c.ref_des, p.number)
            rng = ctx.rail_range(n) if n else None
            if rng:
                vmax = max(vmax, rng[1])
        if vmax > rating:
            sev = Severity.ERROR
        elif vmax * 1.5 > rating:
            sev = Severity.WARNING
        else:
            continue
        out.append(
            F(
                "ERC-CAP-VOLTAGE",
                c.ref_des,
                sev,
                f"{c.ref_des} rated {rating:g} V sits on up to {vmax:g} V "
                "(ceramics lose capacitance with DC bias; keep >= 1.5x).",
                refs=[c.ref_des],
            )
        )
    return out


def rule_unverified(ctx: Ctx) -> list[Finding]:
    return [
        F(
            "ERC-UNVERIFIED-EXCLUDED",
            c.ref_des,
            Severity.WARNING,
            f"{c.ref_des} ({c.mpn}) has no verified pin table and is not in the netlist; "
            "ERC cannot check it.",
            refs=[c.ref_des],
        )
        for c in ctx.plan.components
        if not c.verified
    ]


RULES: list[Callable[[Ctx], list[Finding]]] = [
    rule_unconnected,
    rule_nc_conflicts,
    rule_single_pin_nets,
    rule_driver_conflicts,
    rule_power_undriven,
    rule_ground_pins,
    rule_supply_range,
    rule_decoupling,
    rule_i2c,
    rule_usb,
    rule_floating_inputs,
    rule_inductive_loads,
    rule_led_resistor,
    rule_cap_voltage,
]


def run_erc(plan: DesignPlan) -> list[Finding]:
    if plan.netlist is None:
        return []
    ctx = Ctx.build(plan)
    out: list[Finding] = []
    for rule in RULES:
        out += rule(ctx)
    return out
