"""Deterministic netlist synthesizer.

Input: the functional parts chosen by the LLM (already resolved against the library) plus
the requirements. Output: the complete component list (functional parts + support
passives) and a pin-level :class:`~pcbassist.schemas.Netlist` in which every pin of every
verified part is either on a net or an explicit no-connect with a reason.

Nothing here is guessed by a language model. Pin numbers come from the KiCad-derived pin
tables in the library; which pin does what comes from the pin *function tags* derived from
KiCad pin names. The circuit rules (decoupling, pull-ups, CC resistors, ...) are written as
code below with the reasoning next to them. Where a subcircuit is not implemented, the
part's remaining pins are left as explicit no-connects AND a warning finding says the
subcircuit must be completed by an engineer -- never silently.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from ..library import ComponentLibrary
from ..schemas import (
    Component,
    CurrentProfile,
    Finding,
    FindingCategory,
    LibraryPart,
    Net,
    NetClassName,
    NetConnection,
    Netlist,
    NoConnect,
    Pin,
    PinType,
    RailConstraint,
    Requirements,
    Severity,
)
from ..schemas.common import ItemStatus

# ---------------------------------------------------------------------------
# Support-part MPNs (all present in the curated library)
# ---------------------------------------------------------------------------

R_0402 = {
    "0": "RC0402JR-070RL",
    "330": "RC0402FR-07330RL",
    "1k": "RC0402FR-071KL",
    "4.7k": "RC0402FR-074K7L",
    "5.1k": "RC0402FR-075K1L",
    "10k": "RC0402FR-0710KL",
    "100k": "RC0402FR-07100KL",
    "1M": "RC0402FR-071ML",
}
R_PROG = {"2k": "RC0603FR-072KL", "10k": "RC0603FR-0710KL"}
FLYBACK_DIODE = "1N4148W-7-F"
CATCH_DIODE = "SS14"
LOW_SIDE_FET = "AO3400A"
BUTTON = "PTS810 SJM 250 SMTR LFS"
BUCK_INDUCTOR_LM2596 = "SRN8040TA-330M"

#: Relay pinouts. KiCad's relay symbols have unnamed pins; the roles below come from the
#: symbol geometry (both coil pins sit on the coil column at x=-5.08 mm, the common blade
#: is the bottom-right pin, the two contacts are the top pins). Which contact is NO and
#: which NC is NOT asserted here -- the report asks the engineer to confirm it.
RELAY_PINOUT: dict[str, dict[str, tuple[str, ...]]] = {
    "SRD-05VDC-SL-C": {"coil": ("2", "5"), "com": ("1",), "contacts": ("3", "4")},
    "G5LE-1-DC12": {"coil": ("2", "5"), "com": ("1",), "contacts": ("3", "4")},
}

#: Extra MCU facts that are not encoded in KiCad pin names. Source: the module vendors'
#: hardware design guidelines (ADC1 channel pins, strapping levels). Pins are by name.
MCU_EXTRA: dict[str, dict[str, list[str]]] = {
    "ESP32-C3-WROOM-02-N4": {
        "adc": ["IO0", "IO1", "IO3", "IO4"],
        "wake": ["IO0", "IO1", "IO3", "IO4", "IO5"],
        # GPIO2 and GPIO8 must read high for SPI boot / download boot.
        "pullup": ["IO2", "IO8"],
        "decoupling": ["10u", "100n"],
    },
    "ESP32-C6-MINI-1-N4": {
        "adc": ["IO0", "IO1", "IO2", "IO3", "IO4", "IO5", "IO6"],
        "pullup": ["IO8"],
        "decoupling": ["10u", "100n"],
    },
    "ESP32-S3-WROOM-1-N8": {
        "adc": [f"IO{i}" for i in range(1, 11)],
        "decoupling": ["10u", "100n"],
    },
    "ESP32-S3-MINI-1-N8": {
        "adc": [f"IO{i}" for i in range(1, 11)],
        "decoupling": ["10u", "100n"],
    },
    "ESP32-WROOM-32E-N4": {
        "adc": ["SENSOR_VP", "SENSOR_VN", "IO34", "IO35", "IO32", "IO33"],
        "decoupling": ["10u", "100n"],
    },
    "MDBT42Q-512KV2": {"swo": ["TRACEDATA0/P0.18"], "decoupling": ["4.7u", "100n"]},
    "E73-2G4M04S1B": {"decoupling": ["4.7u", "100n"]},
    "MDBT50Q-1MV2": {"decoupling": ["4.7u", "100n"]},
}

#: Typical-application capacitors per regulator (input list, output list). Values follow
#: the datasheets' typical application circuits and must be checked by the reviewer.
REGULATOR_CAPS: dict[str, tuple[list[str], list[str]]] = {
    "AP2112K-3.3TRG1": (["1u"], ["1u"]),
    "MCP1700T-3302E/TT": (["1u"], ["1u"]),
    "LP5907MFX-3.3/NOPB": (["1u"], ["1u"]),
    "AP7361C-33E-13": (["1u"], ["1u"]),
    "AMS1117-3.3": (["10u"], ["22u"]),
    "L7805CV": (["100n"], ["100n", "10u"]),
    "LM2596S-5.0/NOPB": (["10u", "100n"], ["22u", "22u"]),
}

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
PASSIVE_CATEGORIES = {"resistor", "capacitor", "inductor", "ferrite", "crystal"}
SENSOR_CATEGORIES = {"sensor_env", "sensor_motion", "sensor_temp", "sensor_light", "rtc"}


class SynthError(RuntimeError):
    """Internal inconsistency in the synthesizer (a bug, not a design problem)."""


@dataclass
class Selected:
    """A functional part chosen upstream (LLM or reviewer)."""

    mpn: str
    role: str
    block_id: str | None
    block_kind: str | None
    rationale: str = ""
    confidence: float = 0.5
    alternates: list[str] = field(default_factory=list)
    origin: str = "llm"
    status: ItemStatus = ItemStatus.PROPOSED
    locked: bool = False
    ref_hint: str | None = None


@dataclass
class SPart:
    key: str
    mpn: str
    lib: LibraryPart | None
    role: str
    block_id: str | None
    block_kind: str | None
    origin: str
    rationale: str = ""
    confidence: float = 0.9
    alternates: list[str] = field(default_factory=list)
    rail: str | None = None
    support_for: str | None = None
    support_pin: str | None = None
    current: CurrentProfile | None = None
    specs: dict[str, str | int | float | bool] = field(default_factory=dict)
    status: ItemStatus = ItemStatus.PROPOSED
    locked: bool = False
    ref_hint: str | None = None
    ref: str = ""

    @property
    def category(self) -> str:
        return self.lib.category if self.lib else "unknown"

    @property
    def pins(self) -> list[Pin]:
        return self.lib.pins if self.lib else []

    def spec(self, name: str) -> float | None:
        v = self.lib.key_specs.get(name) if self.lib else None
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


@dataclass
class Rail:
    name: str
    nominal_v: float
    min_v: float
    max_v: float
    kind: str
    source_key: str | None = None
    parent: str | None = None
    max_current_ma: float | None = None


@dataclass
class SynthResult:
    components: list[Component]
    netlist: Netlist
    rails: list[RailConstraint]
    findings: list[Finding]
    notes: list[str]


def _natural(s: str) -> list[object]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _fmt_v(v: float) -> str:
    return f"{v:g}"


def rail_name_for(v: float) -> str:
    """'+3V3' for 3.3 V, '+5V' for 5 V, '+1V8' for 1.8 V."""
    whole = int(v)
    frac = round((v - whole) * 10)
    return f"+{whole}V{frac}" if frac else f"+{whole}V"


def parse_i2c_addresses(spec: object) -> list[int]:
    """Parse '0x44', '0x76/0x77 (SDO)', '0x48-0x4B (ADD0)' into address lists."""
    if not isinstance(spec, str):
        return []
    hexes = [int(h, 16) for h in re.findall(r"0x([0-9A-Fa-f]{2})", spec)]
    if re.search(r"0x[0-9A-Fa-f]{2}\s*-\s*0x[0-9A-Fa-f]{2}", spec) and len(hexes) >= 2:
        return list(range(hexes[0], hexes[1] + 1))
    return hexes


class Circuit:
    """Mutable netlist under construction. Pins are referenced as (part key, pin number)."""

    def __init__(self, lib: ComponentLibrary) -> None:
        self.lib = lib
        self.parts: dict[str, SPart] = {}
        self.order: list[str] = []
        self.pin_net: dict[tuple[str, str], str] = {}
        self.net_members: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.net_class: dict[str, NetClassName] = {}
        self.net_voltage: dict[str, float] = {}
        self.no_connects: dict[tuple[str, str], str] = {}
        self.findings: list[Finding] = []
        self.notes: list[str] = []
        self.rails: dict[str, Rail] = {}
        self._key_counts: dict[str, int] = defaultdict(int)

    # -- parts ---------------------------------------------------------------
    def unique_key(self, base: str) -> str:
        n = self._key_counts[base]
        self._key_counts[base] += 1
        return base if n == 0 else f"{base}#{n}"

    def add(
        self,
        mpn: str,
        role: str,
        *,
        key: str,
        block_id: str | None = None,
        block_kind: str | None = None,
        origin: str = "synth",
        rationale: str = "",
        confidence: float = 0.9,
        rail: str | None = None,
        support_for: SPart | None = None,
        support_pin: str | None = None,
    ) -> SPart:
        part = self.lib.get(mpn)
        if part is None:
            raise SynthError(f"support part {mpn} missing from library")
        key = self.unique_key(key)
        sp = SPart(
            key=key,
            mpn=part.mpn,
            lib=part,
            role=role,
            block_id=block_id if block_id else (support_for.block_id if support_for else None),
            block_kind=block_kind
            if block_kind
            else (support_for.block_kind if support_for else None),
            origin=origin,
            rationale=rationale,
            confidence=confidence,
            rail=rail,
            support_for=support_for.key if support_for else None,
            support_pin=support_pin,
        )
        self.parts[key] = sp
        self.order.append(key)
        return sp

    def register(self, sp: SPart) -> SPart:
        self.parts[sp.key] = sp
        self.order.append(sp.key)
        return sp

    # -- connectivity ----------------------------------------------------------
    def connect(
        self,
        net: str,
        part: SPart,
        pin: str,
        *,
        net_class: NetClassName | None = None,
        voltage: float | None = None,
    ) -> None:
        if part.lib is None:
            raise SynthError(f"cannot connect unverified part {part.key}")
        if not any(p.number == pin for p in part.pins):
            raise SynthError(f"{part.mpn} has no pin {pin}")
        k = (part.key, pin)
        prev = self.pin_net.get(k)
        if prev == net:
            return
        if prev is not None:
            raise SynthError(f"{part.mpn} pin {pin} already on {prev}, cannot also join {net}")
        if k in self.no_connects:
            raise SynthError(f"{part.mpn} pin {pin} is marked no-connect")
        self.pin_net[k] = net
        self.net_members[net].append(k)
        if net_class is not None:
            self.net_class.setdefault(net, net_class)
        if voltage is not None:
            self.net_voltage.setdefault(net, voltage)

    def connect_all(self, net: str, part: SPart, pins: Iterable[Pin], **kw: object) -> None:
        for p in pins:
            self.connect(net, part, p.number, **kw)  # type: ignore[arg-type]

    def nc(self, part: SPart, pin: str, reason: str) -> None:
        k = (part.key, pin)
        if k in self.pin_net or k in self.no_connects:
            return
        self.no_connects[k] = reason

    def free(self, part: SPart, pin: Pin) -> bool:
        k = (part.key, pin.number)
        return k not in self.pin_net and k not in self.no_connects

    def pins(
        self,
        part: SPart,
        *,
        func: str | None = None,
        name: str | None = None,
        ptype: PinType | None = None,
        free_only: bool = False,
    ) -> list[Pin]:
        out = []
        for p in part.pins:
            if func is not None and func not in p.functions:
                continue
            if name is not None and p.name != name:
                continue
            if ptype is not None and p.type != ptype:
                continue
            if free_only and not self.free(part, p):
                continue
            out.append(p)
        return out

    def power_pins(self, part: SPart) -> list[Pin]:
        """Supply inputs: power_in pins that are not ground, battery-sense or reg outputs."""
        return [
            p
            for p in part.pins
            if p.type == PinType.POWER_IN
            and "gnd" not in p.functions
            and "internal_regulator" not in p.functions
        ]

    def ground(self, part: SPart) -> None:
        for p in self.pins(part, func="gnd"):
            self.connect("GND", part, p.number, net_class=NetClassName.GROUND, voltage=0.0)

    # -- rails ---------------------------------------------------------------
    def add_rail(self, rail: Rail) -> Rail:
        self.rails[rail.name] = rail
        self.net_class[rail.name] = NetClassName.POWER
        self.net_voltage[rail.name] = rail.nominal_v
        return rail

    def finding(
        self,
        rule: str,
        subject: str,
        severity: Severity,
        message: str,
        *,
        category: FindingCategory = FindingCategory.ERC,
        refs: list[str] | None = None,
        fix: str | None = None,
    ) -> None:
        self.findings.append(
            Finding(
                id=f"{rule}:{subject}",
                rule=rule,
                severity=severity,
                category=category,
                message=message,
                refs=refs or [],
                suggested_fix=fix,
            )
        )

    # -- capacitor / resistor helpers -------------------------------------------
    @staticmethod
    def _pin_label(part: SPart, pin: str | None) -> str | None:
        """Pin *name* for stable keys (VDD stays VDD when a part is swapped)."""
        if pin is None:
            return None
        p = next((x for x in part.pins if x.number == pin), None)
        return p.name if p is not None and p.name else pin

    def pick_cap(self, value: str, rail_v: float) -> str:
        """Library capacitor with this value rated >= 1.5x the rail voltage (else best)."""
        caps = [p for p in self.lib.all() if p.category == "capacitor"]

        def farads(p: LibraryPart) -> float:
            v = p.key_specs.get("capacitance_f", 0.0)
            return float(v) if isinstance(v, (int, float)) else 0.0

        def volts(p: LibraryPart) -> float:
            v = p.key_specs.get("voltage_v", 0.0)
            return float(v) if isinstance(v, (int, float)) else 0.0

        exact = sorted((p for p in caps if p.value == value), key=lambda p: (volts(p), p.mpn))
        ok = [p for p in exact if volts(p) >= 1.5 * rail_v]
        if ok:
            return ok[0].mpn
        if exact:
            want = farads(exact[0])
            bigger = sorted(
                (p for p in caps if farads(p) >= want and volts(p) >= 1.5 * rail_v),
                key=lambda p: (farads(p), volts(p)),
            )
            if bigger and farads(bigger[0]) <= want * 10:
                return bigger[0].mpn
            return exact[-1].mpn
        raise SynthError(f"no capacitor with value {value} in library")

    def cap(
        self,
        value: str,
        net: str,
        anchor: SPart,
        anchor_pin: str | None,
        purpose: str,
        *,
        gnd: str = "GND",
    ) -> SPart:
        v = self.net_voltage.get(net, 3.3)
        c = self.add(
            self.pick_cap(value, v),
            f"{purpose} ({value}) for {anchor.role}",
            key=(
                f"{anchor.key}/{purpose.split()[0].lower()}:"
                f"{self._pin_label(anchor, anchor_pin) or net}:{value}"
            ),
            support_for=anchor,
            support_pin=anchor_pin,
            rationale=f"{purpose} on {net}",
            rail=None,
        )
        self.connect(net, c, "1")
        self.connect(gnd, c, "2", net_class=NetClassName.GROUND, voltage=0.0)
        return c

    def res(
        self,
        value: str,
        net_a: str,
        net_b: str,
        anchor: SPart,
        anchor_pin: str | None,
        purpose: str,
        *,
        mpn: str | None = None,
    ) -> SPart:
        r = self.add(
            mpn or R_0402[value],
            f"{purpose} ({value})",
            key=(
                f"{anchor.key}/{purpose.split()[0].lower()}:"
                f"{self._pin_label(anchor, anchor_pin) or net_a}"
            ),
            support_for=anchor,
            support_pin=anchor_pin,
            rationale=purpose,
        )
        self.connect(net_a, r, "1")
        self.connect(net_b, r, "2")
        return r


# ---------------------------------------------------------------------------
# GPIO allocation
# ---------------------------------------------------------------------------

_RESERVED_TAGS = {
    "strapping",
    "boot",
    "usb_dp",
    "usb_dm",
    "uart_tx",
    "uart_rx",
    "crystal",
    "nfc",
    "swd",
    "reset",
    "enable",
}


class GpioAllocator:
    def __init__(self, circuit: Circuit, mcu: SPart) -> None:
        self.c = circuit
        self.mcu = mcu
        extra = MCU_EXTRA.get(mcu.mpn, {})
        self.adc = list(extra.get("adc", []))
        self.wake = list(extra.get("wake", []))
        reserved = set(extra.get("pullup", [])) | set(extra.get("swo", []))
        self.pool = sorted(
            (
                p
                for p in mcu.pins
                if "gpio" in p.functions
                and not (_RESERVED_TAGS & set(p.functions))
                and p.name not in reserved
            ),
            key=lambda p: _natural(p.number),
        )
        self.assigned: dict[str, str] = {}
        self.taken: set[str] = set()

    def take(
        self, signal: str, *, adc: bool = False, wake: bool = False, output: bool = False
    ) -> Pin | None:
        def usable(p: Pin) -> bool:
            if not self.c.free(self.mcu, p) or p.number in self.taken:
                return False
            if "input_only" in p.functions and output:
                return False
            return not (adc and "adc" not in p.functions and p.name not in self.adc)

        pool = [p for p in self.pool if usable(p)]
        if adc:
            pref = [p for p in pool if p.name in self.adc or "adc" in p.functions]
        elif wake and self.wake:
            pref = [p for p in pool if p.name in self.wake]
        else:
            # keep ADC- and wake-capable pins for the requests that need them
            special = set(self.adc) | set(self.wake)
            pref = [p for p in pool if p.name not in special and "adc" not in p.functions]
            # input-only pins last for outputs, first-come otherwise
        choice = pref[0] if pref else (pool[0] if pool else None)
        if choice is not None:
            self.assigned[signal] = choice.name
            self.taken.add(choice.number)
        return choice


# ---------------------------------------------------------------------------
# The synthesizer
# ---------------------------------------------------------------------------


class Synthesizer:
    def __init__(
        self,
        lib: ComponentLibrary,
        requirements: Requirements | None,
        selected: list[Selected],
        previous_refs: dict[str, str] | None = None,
    ) -> None:
        self.lib = lib
        self.req = requirements
        self.c = Circuit(lib)
        self.selected = selected
        self.previous_refs = previous_refs or {}
        self.unverified: list[SPart] = []
        self.mcu: SPart | None = None
        self.gpio: GpioAllocator | None = None
        self.mcu_rail: str | None = None
        self.i2c_devices: list[SPart] = []

    # -- entry ---------------------------------------------------------------
    def run(self) -> SynthResult:
        self._register_selected()
        self._power_sources()
        self._chargers()
        self._regulators()
        self._choose_mcu_rail()
        self._load_switches()
        self._mcu()
        self._usb()
        self._i2c()
        self._debug()
        self._user_io()
        self._actuators()
        self._battery_sense()
        self._headers()
        self._generic_leftovers()
        self._finalize_mcu()
        return self._finalize()

    # -- registration ------------------------------------------------------------
    def _register_selected(self) -> None:
        for s in self.selected:
            lp = self.lib.get(s.mpn)
            base = f"{s.block_id or 'misc'}/{lp.category if lp else 'unverified'}"
            key = self.c.unique_key(base)
            sp = SPart(
                key=key,
                mpn=lp.mpn if lp else s.mpn,
                lib=lp,
                role=s.role,
                block_id=s.block_id,
                block_kind=s.block_kind,
                origin=s.origin,
                rationale=s.rationale,
                confidence=s.confidence if lp else min(s.confidence, 0.3),
                alternates=list(s.alternates),
                status=s.status,
                locked=s.locked,
                ref_hint=s.ref_hint,
            )
            if lp is None:
                self.unverified.append(sp)
            self.c.register(sp)

    def of(self, *categories: str) -> list[SPart]:
        return [
            self.c.parts[k]
            for k in self.c.order
            if self.c.parts[k].lib is not None
            and self.c.parts[k].category in categories
            and self.c.parts[k].origin != "synth"
        ]

    def _role(self, sp: SPart, *words: str) -> bool:
        text = f"{sp.role} {sp.block_id or ''} {sp.block_kind or ''}".lower()
        return any(w in text for w in words)

    def _source(self, *kinds: str) -> tuple[float, float, float, float | None] | None:
        if self.req is None:
            return None
        for ps in self.req.power_sources:
            if ps.kind in kinds:
                return ps.nominal_v, ps.min_v, ps.max_v, ps.max_current_ma
        return None

    # -- power entry -----------------------------------------------------------
    def _protect_chain(self, raw: str, final: str, v: float, block: str | None) -> str:
        """Fuse and series diode between the connector net and the protected rail."""
        c = self.c
        fuses = [f for f in self.of("fuse") if f.block_id == block or not block]
        diodes = [
            d
            for d in self.of("diode")
            if self._role(d, "reverse", "polarity", "series", "oring", "input")
            and (d.block_id == block or not block)
        ]
        tvs = [t for t in self.of("tvs") if t.block_id == block or not block]
        net = raw
        steps = [(f, "fuse") for f in fuses[:1]] + [(d, "diode") for d in diodes[:1]]
        for i, (part, kind) in enumerate(steps):
            nxt = final if i == len(steps) - 1 else f"{final}_F"
            if kind == "fuse":
                c.connect(net, part, "1", net_class=NetClassName.POWER, voltage=v)
                c.connect(nxt, part, "2", net_class=NetClassName.POWER, voltage=v)
                part.rail = final
            else:
                a = c.pins(part, func="anode")[0]
                k = c.pins(part, func="cathode")[0]
                c.connect(net, part, a.number, net_class=NetClassName.POWER, voltage=v)
                c.connect(nxt, part, k.number, net_class=NetClassName.POWER, voltage=v)
                part.rail = final
                c.notes.append(
                    f"{part.mpn} in series with {final} for reverse-polarity protection "
                    f"(drops ~{part.spec('vf_v') or 0.5:g} V)."
                )
            net = nxt
        for t in tvs[:1]:
            # Unidirectional TVS on D_SMA: pad 1 is the cathode (band) -> rail.
            c.connect(final if steps else raw, t, "1")
            c.connect("GND", t, "2", net_class=NetClassName.GROUND, voltage=0.0)
            t.rail = final
        return final if steps else raw

    def _power_sources(self) -> None:
        c = self.c
        for j in self.of("connector_usb"):
            src = self._source("usb_5v")
            nominal, vmin, vmax, imax = src or (5.0, 4.75, 5.25, 500.0)
            raw = "VBUS_RAW" if self.of("fuse") and self._usb_fused(j) else "VBUS"
            for p in c.pins(j, func="vbus"):
                c.connect(raw, j, p.number, net_class=NetClassName.POWER, voltage=nominal)
            c.ground(j)
            for p in c.pins(j, func="shield"):
                c.connect("GND", j, p.number)
            final = self._protect_chain(raw, "VBUS", nominal, j.block_id) if raw != "VBUS" else raw
            c.add_rail(
                Rail(final, nominal, vmin, vmax, "usb", source_key=j.key, max_current_ma=imax)
            )
            if raw != "VBUS":
                c.net_class[raw] = NetClassName.POWER
                c.net_voltage[raw] = nominal
            j.rail = final
            break
        for j in self.of("connector_power") + [
            t
            for t in self.of("connector_terminal")
            if t.block_kind == "power_input" or self._role(t, "power in", "supply", "input")
        ]:
            src = self._source("dc_jack", "dc_terminal", "other")
            if src is None:
                src = (12.0, 11.0, 13.0, None)
                c.notes.append(
                    "DC input assumed 12 V nominal (11-13 V); no source in requirements."
                )
            nominal, vmin, vmax, imax = src
            pos = j.pins[0]
            neg = j.pins[1]
            c.connect("VIN_RAW", j, pos.number, net_class=NetClassName.POWER, voltage=nominal)
            c.connect("GND", j, neg.number, net_class=NetClassName.GROUND, voltage=0.0)
            for p in j.pins[2:]:
                c.nc(j, p.number, "mechanical mounting pin")
            final = self._protect_chain("VIN_RAW", "VIN", nominal, j.block_id)
            drop = 0.0
            for d in self.of("diode"):
                if d.rail == "VIN":
                    drop = d.spec("vf_v") or 0.5
            c.add_rail(
                Rail(
                    final,
                    nominal - drop,
                    vmin - drop,
                    vmax,
                    "dc_input",
                    source_key=j.key,
                    max_current_ma=imax,
                )
            )
            j.rail = final
            if self.of("connector_power"):
                c.notes.append(
                    f"{j.mpn}: centre pin (symbol pin 1) assumed positive; "
                    "confirm adapter polarity."
                )
            break
        for b in self.of("connector_battery", "battery_holder"):
            src = self._source("li_ion_1s", "lipo_1s", "coin_cell", "alkaline_aa", "alkaline_aaa")
            if src is None:
                if b.category == "battery_holder":
                    src = (3.0, 2.0, 3.0, 15.0)
                else:
                    src = (3.7, 3.0, 4.2, None)
                c.notes.append(
                    f"Battery parameters not in requirements; assumed {src[1]}-{src[2]} V."
                )
            nominal, vmin, vmax, imax = src
            pos_pins = c.pins(b, func="positive") or [b.pins[0]]
            neg_pins = c.pins(b, func="negative") or [b.pins[1]]
            c.connect("VBAT", b, pos_pins[0].number, net_class=NetClassName.POWER, voltage=nominal)
            c.connect("GND", b, neg_pins[0].number, net_class=NetClassName.GROUND, voltage=0.0)
            for p in b.pins:
                if c.free(b, p):
                    c.nc(b, p.number, "mechanical mounting pad")
            c.add_rail(
                Rail("VBAT", nominal, vmin, vmax, "battery", source_key=b.key, max_current_ma=imax)
            )
            b.rail = "VBAT"
            if b.category == "connector_battery":
                c.notes.append(
                    f"{b.mpn}: pin 1 = battery +, pin 2 = battery -. JST-PH battery lead "
                    "polarity is NOT standardised between vendors - check before connecting."
                )
            break
        for e in self.of("esd"):
            if e.mpn == "USBLC6-2SC6":
                continue  # handled with the USB data lines
            if c.free(e, e.pins[0]):
                c.nc(e, e.pins[0].number, "ESD diode: no protected line chosen")
                c.nc(e, e.pins[1].number, "ESD diode: no protected line chosen")
                c.finding(
                    "SYNTH-ESD-UNUSED",
                    e.key,
                    Severity.WARNING,
                    f"{e.mpn} ({e.role}) was selected but the synthesizer did not know which "
                    "line it protects; it is unconnected.",
                    refs=[e.key],
                )

    def _usb_fused(self, j: SPart) -> bool:
        return any(f.block_id == j.block_id for f in self.of("fuse"))

    # -- charging ----------------------------------------------------------------
    def _chargers(self) -> None:
        c = self.c
        for ch in self.of("charger"):
            if ch.mpn != "MCP73831T-2ACI/OT":
                self._generic(ch, reason="charger subcircuit not implemented for this part")
                continue
            vin = "VBUS" if "VBUS" in c.rails else next(iter(c.rails), "VBUS")
            c.connect(vin, ch, c.pins(ch, name="VDD")[0].number)
            c.connect("VBAT", ch, c.pins(ch, name="VBAT")[0].number)
            c.ground(ch)
            ch.rail = vin
            c.cap("4.7u", vin, ch, c.pins(ch, name="VDD")[0].number, "Input capacitor")
            c.cap("4.7u", "VBAT", ch, c.pins(ch, name="VBAT")[0].number, "Output capacitor")
            cap_mah = self._battery_capacity()
            # I_REG = 1000 / R_PROG(kOhm); pick <= 0.5C from the available resistors.
            choice = "10k"
            if cap_mah is None or cap_mah >= 1000:
                choice = "2k"
            i_reg = 1000 / float(choice.rstrip("k"))
            prog = c.pins(ch, name="PROG")[0].number
            c.res(
                choice,
                "CHG_PROG",
                "GND",
                ch,
                prog,
                f"Charge-current program resistor, I_REG = 1000/R = {i_reg:g} mA",
                mpn=R_PROG[choice],
            )
            c.connect("CHG_PROG", ch, prog, net_class=NetClassName.ANALOG)
            ch.specs["charge_current_ma"] = i_reg
            if cap_mah:
                c.notes.append(
                    f"Charge current {i_reg:g} mA = {i_reg / cap_mah:.2f}C for {cap_mah:g} mAh."
                )
            stat = c.pins(ch, name="STAT")[0].number
            leds = [x for x in self.of("led") if self._role(x, "charg")]
            if leds:
                led = leds[0]
                r = c.res(
                    "1k", vin, "CHG_LED", led, "2", "Charge LED series resistor"
                )  # anode side
                del r
                c.connect("CHG_LED", led, c.pins(led, func="anode")[0].number)
                c.connect("CHG_STAT", led, c.pins(led, func="cathode")[0].number)
                c.connect("CHG_STAT", ch, stat)
                vf = led.spec("vf_v") or 2.0
                led.rail = vin
                i = (self._v(vin) - vf) / 1.0
                # Indicator LEDs: counted at peak, not in the average (on-time is app-specific).
                led.current = CurrentProfile(typ_ma=0.0, max_ma=round(i, 2), sleep_ua=0)
            else:
                c.nc(ch, stat, "charge status output unused")

    def _battery_capacity(self) -> float | None:
        if self.req is None:
            return None
        for ps in self.req.power_sources:
            if ps.capacity_mah:
                return ps.capacity_mah
        return None

    def _v(self, net: str) -> float:
        r = self.c.rails.get(net)
        if r:
            return r.nominal_v
        return self.c.net_voltage.get(net, 3.3)

    # -- regulation --------------------------------------------------------------
    def _regulators(self) -> None:
        c = self.c
        regs = self.of("ldo", "buck")
        regs.sort(key=lambda r: -(r.spec("vout_v") or 0))
        for reg in regs:
            vout = reg.spec("vout_v")
            if vout is None or reg.mpn not in REGULATOR_CAPS:
                self._generic(reg, reason="adjustable/unsupported regulator topology")
                continue
            dropout = (reg.spec("dropout_mv") or 300) / 1000
            parent = self._pick_input_rail(reg, vout, dropout)
            out = rail_name_for(vout)
            vin_pins = [
                p for p in reg.pins if p.type == PinType.POWER_IN and "gnd" not in p.functions
            ]
            vout_pins = [p for p in reg.pins if p.type == PinType.POWER_OUT] or [
                p for p in reg.pins if p.name == "OUT"
            ]
            c.ground(reg)
            for p in vin_pins:
                c.connect(parent, reg, p.number)
            reg.rail = parent
            in_caps, out_caps = REGULATOR_CAPS[reg.mpn]
            for v in in_caps:
                c.cap(v, parent, reg, vin_pins[0].number, "Input capacitor")
            if reg.category == "ldo":
                for p in vout_pins:
                    c.connect(out, reg, p.number, net_class=NetClassName.POWER, voltage=vout)
                for p in c.pins(reg, func="enable"):
                    c.connect(parent, reg, p.number)
                    c.notes.append(f"{reg.mpn} enable tied to its input: always on.")
            else:  # fixed-output buck (LM2596-5.0 style)
                sw = f"SW_{out.strip('+')}"
                c.connect(sw, reg, vout_pins[0].number, net_class=NetClassName.POWER, voltage=vout)
                ind = c.add(
                    BUCK_INDUCTOR_LM2596,
                    f"Buck inductor for {reg.role}",
                    key=f"{reg.key}/inductor",
                    support_for=reg,
                    support_pin=vout_pins[0].number,
                    rationale="33 uH per the LM2596 typical application for 5 V output",
                )
                c.connect(sw, ind, "1")
                c.connect(out, ind, "2", net_class=NetClassName.POWER, voltage=vout)
                dio = c.add(
                    CATCH_DIODE,
                    f"Catch (freewheel) diode for {reg.role}",
                    key=f"{reg.key}/catch",
                    support_for=reg,
                    support_pin=vout_pins[0].number,
                    rationale="Schottky freewheel diode required by the asynchronous buck",
                )
                c.connect(sw, dio, c.pins(dio, func="cathode")[0].number)
                c.connect("GND", dio, c.pins(dio, func="anode")[0].number)
                for p in c.pins(reg, func="feedback"):
                    c.connect(out, reg, p.number)
                for p in reg.pins:
                    if p.name in ("~ON/OFF",):
                        c.connect("GND", reg, p.number)
                        c.notes.append(f"{reg.mpn} ~ON/OFF tied low: always on.")
                c.notes.append(
                    f"{reg.mpn}: the datasheet design uses low-ESR electrolytic output "
                    "capacitance; the ceramic-only output here must be checked for loop "
                    "stability. Catch diode SS14 is rated 1 A."
                )
            for v in out_caps:
                c.cap(v, out, reg, vout_pins[0].number, "Output capacitor")
            for p in c.pins(reg, func="nc"):
                c.nc(reg, p.number, "NC per KiCad symbol")
            imax = reg.spec("iout_max_ma")
            kind = "ldo" if reg.category == "ldo" else "buck"
            if out in c.rails:
                c.finding(
                    "SYNTH-RAIL-DUP",
                    out,
                    Severity.ERROR,
                    f"Two regulators drive {out}; outputs would be shorted.",
                    refs=[reg.key],
                )
            c.add_rail(
                Rail(
                    out,
                    vout,
                    round(vout * 0.97, 3),
                    round(vout * 1.03, 3),
                    kind,
                    source_key=reg.key,
                    parent=parent,
                    max_current_ma=imax,
                )
            )

    def _pick_input_rail(self, reg: SPart, vout: float, dropout: float) -> str:
        rails = [r for r in self.c.rails.values() if r.name != rail_name_for(vout)]
        sr = reg.lib.supply_range if reg.lib else None
        fits = [
            r
            for r in rails
            if r.nominal_v - dropout >= vout and (sr is None or (sr.contains(r.max_v)))
        ]
        if fits:
            return min(fits, key=lambda r: r.nominal_v).name
        if not rails:
            self.c.add_rail(Rail("VIN", 5.0, 4.5, 5.5, "other"))
            self.c.notes.append(f"No supply found for {reg.mpn}; created placeholder VIN.")
            return "VIN"
        # Nothing fits: pick the highest rail the part tolerates; power checks report it.
        tolerated = [r for r in rails if sr is None or r.max_v <= sr.max_v] or rails
        return max(tolerated, key=lambda r: r.nominal_v).name

    def _choose_mcu_rail(self) -> None:
        mcus = self.of("mcu_wireless", "mcu")
        if not mcus:
            self.mcu_rail = next(
                (r for r in self.c.rails if r.startswith("+3V3")), next(iter(self.c.rails), None)
            )
            return
        self.mcu = mcus[0]
        for extra in mcus[1:]:
            self._generic(extra, reason="only one MCU is wired by the synthesizer")
        sr = self.mcu.lib.supply_range if self.mcu.lib else None
        rails = list(self.c.rails.values())
        regulated = [r for r in rails if r.kind in ("ldo", "buck", "boost")]
        for group in (regulated, rails):
            ok = [r for r in group if sr is None or sr.contains(r.nominal_v)]
            if ok:
                self.mcu_rail = min(ok, key=lambda r: abs(r.nominal_v - 3.3)).name
                return
        self.mcu_rail = (regulated or rails)[0].name if (regulated or rails) else None
        if self.mcu_rail is None:
            self.c.add_rail(Rail("+3V3", 3.3, 3.2, 3.4, "other"))
            self.mcu_rail = "+3V3"
            self.c.notes.append("No supply rail could be derived; placeholder +3V3 created.")

    def _load_switches(self) -> None:
        c = self.c
        for ls in self.of("load_switch"):
            if ls.mpn != "TPS22917DBVR" or self.mcu_rail is None:
                self._generic(ls, reason="load switch subcircuit not implemented")
                continue
            vin = self.mcu_rail
            out = f"{vin}_SW"
            c.connect(vin, ls, c.pins(ls, name="VIN")[0].number)
            c.connect(out, ls, c.pins(ls, name="VOUT")[0].number)
            c.ground(ls)
            ls.rail = vin
            r = c.rails[vin]
            c.add_rail(
                Rail(
                    out,
                    r.nominal_v,
                    r.min_v,
                    r.max_v,
                    "load_switch",
                    ls.key,
                    parent=vin,
                    max_current_ma=(ls.spec("iout_max_a") or 1) * 1000,
                )
            )
            c.cap("1u", vin, ls, c.pins(ls, name="VIN")[0].number, "Input capacitor")
            c.cap("100n", out, ls, c.pins(ls, name="VOUT")[0].number, "Output capacitor")
            c.nc(ls, c.pins(ls, name="CT")[0].number, "slew-rate capacitor not fitted")
            c.nc(ls, c.pins(ls, name="QOD")[0].number, "quick output discharge unused")
            ls.specs["switched_rail"] = out

    def _sensor_rail(self) -> str | None:
        for r in self.c.rails.values():
            if r.kind == "load_switch":
                return r.name
        return self.mcu_rail

    # -- MCU -------------------------------------------------------------------
    def _mcu(self) -> None:
        c = self.c
        mcu = self.mcu
        if mcu is None or self.mcu_rail is None:
            return
        rail = self.mcu_rail
        mcu.rail = rail
        c.ground(mcu)
        pw = c.power_pins(mcu)
        for p in pw:
            if "vbus" in p.functions:
                continue
            c.connect(rail, mcu, p.number)
        extra = MCU_EXTRA.get(mcu.mpn, {})
        first = next((p for p in pw if "vbus" not in p.functions), None)
        if first is not None:
            for v in extra.get("decoupling", ["10u", "100n"]):
                c.cap(v, rail, mcu, first.number, "Decoupling")
        self.gpio = GpioAllocator(c, mcu)
        # Enable/reset RC (ESP32 family: EN must rise after the supply is stable).
        for p in c.pins(mcu, func="enable"):
            c.res("10k", rail, "MCU_EN", mcu, p.number, "EN pull-up")
            c.connect("MCU_EN", mcu, p.number)
            c.cap("1u", "MCU_EN", mcu, p.number, "EN reset-delay capacitor")
        for name in extra.get("pullup", []):
            pin = c.pins(mcu, name=name)
            if pin:
                net = f"STRAP_{name}"
                c.res("10k", rail, net, mcu, pin[0].number, f"Strapping pull-up {name}")
                c.connect(net, mcu, pin[0].number)
        # Boot/download button on the boot strapping pin.
        boot = c.pins(mcu, func="boot")
        buttons = self.of("switch")
        if boot:
            btn = next((b for b in buttons if self._role(b, "boot", "download", "flash")), None)
            if btn is None:
                btn = c.add(
                    BUTTON,
                    "BOOT button (hold during reset for download mode)",
                    key=f"{mcu.key}/boot_button",
                    support_for=mcu,
                    support_pin=boot[0].number,
                    rationale="boot strapping pin needs a way to be pulled low",
                )
            c.connect("MCU_BOOT", mcu, boot[0].number)
            c.connect("MCU_BOOT", btn, "1")
            c.connect("GND", btn, "2")
            btn.rail = rail
        has_usb = bool(c.pins(mcu, func="usb_dp")) and bool(self.of("connector_usb"))
        en = c.pins(mcu, func="enable")
        if en and not has_usb:
            btn = next((b for b in buttons if self._role(b, "reset", " en ", "enable")), None)
            if btn is None:
                btn = c.add(
                    BUTTON,
                    "RESET (EN) button",
                    key=f"{mcu.key}/reset_button",
                    support_for=mcu,
                    support_pin=en[0].number,
                    rationale="no USB auto-reset; manual reset needed for programming",
                )
            c.connect("MCU_EN", btn, "1")
            c.connect("GND", btn, "2")
            btn.rail = rail

    # -- USB ---------------------------------------------------------------------
    def _usb(self) -> None:
        c = self.c
        conns = self.of("connector_usb")
        if not conns:
            return
        j = conns[0]
        for p in c.pins(j, func="usb_cc"):
            net = f"USB_{p.name}"
            c.connect(net, j, p.number)
            c.res("5.1k", net, "GND", j, p.number, f"{p.name} pull-down (UFP/sink advertisement)")
        for p in c.pins(j, func="usb_sbu"):
            c.nc(j, p.number, "SBU unused (USB 2.0)")
        for p in c.pins(j, ptype=PinType.PASSIVE):
            if c.free(j, p) and p.name == "ID":
                c.nc(j, p.number, "ID unused (device)")
        target: SPart | None = None
        if self.mcu is not None and c.pins(self.mcu, func="usb_dp"):
            target = self.mcu
        else:
            uarts = self.of("usb_uart")
            target = uarts[0] if uarts else None
        dp, dm = c.pins(j, func="usb_dp"), c.pins(j, func="usb_dm")
        if target is None:
            for p in dp + dm:
                c.nc(j, p.number, "USB data unused (power-only port)")
            return
        for p in dp:
            c.connect("USB_DP", j, p.number, net_class=NetClassName.HIGH_SPEED)
        for p in dm:
            c.connect("USB_DM", j, p.number, net_class=NetClassName.HIGH_SPEED)
        c.connect("USB_DP", target, c.pins(target, func="usb_dp")[0].number)
        c.connect("USB_DM", target, c.pins(target, func="usb_dm")[0].number)
        for e in self.of("esd"):
            if e.mpn == "USBLC6-2SC6":
                for p in e.pins:
                    if p.name == "I/O1":
                        c.connect("USB_DP", e, p.number)
                    elif p.name == "I/O2":
                        c.connect("USB_DM", e, p.number)
                    elif "vbus" in p.functions:
                        c.connect(j.rail or "VBUS", e, p.number)
                c.ground(e)
                e.rail = j.rail
                break

    # -- I2C ---------------------------------------------------------------------
    def _i2c(self) -> None:
        c = self.c
        devices = [
            d
            for d in self.of(*SENSOR_CATEGORIES, "fuel_gauge")
            if c.pins(d, func="i2c_sda") and c.pins(d, func="i2c_scl")
        ]
        if not devices:
            return
        if self.mcu is None or self.gpio is None:
            for d in devices:
                self._generic(d, reason="no MCU to attach the I2C bus to")
            return
        rail = self._sensor_rail() or self.mcu_rail or "+3V3"
        sda = self.gpio.take("I2C_SDA")
        scl = self.gpio.take("I2C_SCL", output=True)
        if sda is None or scl is None:
            c.finding(
                "SYNTH-GPIO",
                "i2c",
                Severity.ERROR,
                "MCU has no free GPIO for the I2C bus.",
                refs=[self.mcu.key],
            )
            return
        c.connect("I2C_SDA", self.mcu, sda.number, net_class=NetClassName.SIGNAL)
        c.connect("I2C_SCL", self.mcu, scl.number, net_class=NetClassName.SIGNAL)
        c.res("4.7k", self.mcu_rail or rail, "I2C_SDA", self.mcu, sda.number, "I2C SDA pull-up")
        c.res("4.7k", self.mcu_rail or rail, "I2C_SCL", self.mcu, scl.number, "I2C SCL pull-up")
        used_addr: dict[str, int] = defaultdict(int)
        for d in devices:
            self._i2c_device(d, rail, used_addr)

    def _i2c_device(self, d: SPart, rail: str, used: dict[str, int]) -> None:
        c = self.c
        d.rail = rail
        c.ground(d)
        sda = c.pins(d, func="i2c_sda")[0]
        scl = c.pins(d, func="i2c_scl")[0]
        c.connect("I2C_SDA", d, sda.number)
        c.connect("I2C_SCL", d, scl.number)
        powers = [p for p in c.power_pins(d) if "battery" not in p.functions]
        for p in powers:
            c.connect(rail, d, p.number)
        seen: set[str] = set()
        for p in powers:
            if p.name in seen:
                continue
            seen.add(p.name)
            c.cap("100n", rail, d, p.number, "Decoupling")
        addrs = parse_i2c_addresses(d.lib.key_specs.get("i2c_address") if d.lib else None)
        addr_pins = [p for p in c.pins(d, func="i2c_addr") if c.free(d, p)]
        idx = used[d.mpn]
        used[d.mpn] += 1
        for bit, p in enumerate(addr_pins):
            high = bool(idx >> bit & 1)
            c.connect(rail if high else "GND", d, p.number)
        if addrs:
            a = addrs[min(idx, len(addrs) - 1)] if addr_pins else addrs[0]
            d.specs["i2c_address_assigned"] = f"0x{a:02X}"
        if c.pins(d, func="i2c_sda") and c.pins(d, func="spi_cs"):
            for p in c.pins(d, func="spi_cs", free_only=True):
                c.connect(rail, d, p.number)
                c.notes.append(f"{d.mpn} {p.name} tied high to select I2C mode.")
        ints = sorted(c.pins(d, func="interrupt", free_only=True), key=lambda p: _natural(p.name))
        if ints and self.gpio is not None and self.mcu is not None:
            pin = self.gpio.take(f"{d.key}_INT", wake=True)
            if pin is not None:
                net = f"INT_{re.sub(r'[^A-Z0-9]', '', d.mpn.upper())[:8]}"
                net = net if net not in c.net_members else f"{net}_{idx + 1}"
                c.connect(net, d, ints[0].number, net_class=NetClassName.SIGNAL)
                c.connect(net, self.mcu, pin.number)
            for p in ints[1:]:
                c.nc(d, p.number, "unused interrupt output")
        for p in d.pins:
            if not c.free(d, p):
                continue
            if p.type == PinType.NC or "nc" in p.functions:
                c.nc(d, p.number, "NC per KiCad symbol")
            elif re.match(r"^ADC\d$", p.name):
                c.nc(d, p.number, "auxiliary ADC input unused")
        self._leftover_warning(d)
        self.i2c_devices.append(d)

    # -- debug -------------------------------------------------------------------
    def _debug(self) -> None:
        c = self.c
        mcu = self.mcu
        for j in self.of("connector_debug"):
            if mcu is None or self.mcu_rail is None:
                self._generic(j, reason="no MCU for the debug connector")
                continue
            swd_dio = c.pins(mcu, name="SWDIO")
            swd_clk = c.pins(mcu, name="SWDCLK") or c.pins(mcu, name="SWCLK")
            rst = c.pins(mcu, func="reset")
            swo_name = MCU_EXTRA.get(mcu.mpn, {}).get("swo", [])
            swo = c.pins(mcu, name=swo_name[0]) if swo_name else []
            if not swd_dio:
                self._generic(j, reason="MCU has no SWD interface")
                continue
            j.rail = self.mcu_rail
            for p in j.pins:
                n = p.name.upper()
                if n in ("VCC", "VTREF"):
                    c.connect(self.mcu_rail, j, p.number)
                elif "gnd" in p.functions or n == "GNDDETECT":
                    c.connect("GND", j, p.number)
                elif n.startswith("SWDIO"):
                    c.connect("SWDIO", j, p.number, net_class=NetClassName.SIGNAL)
                    c.connect("SWDIO", mcu, swd_dio[0].number)
                elif n.startswith("SWCLK") or n.startswith("SWDCLK"):
                    c.connect("SWCLK", j, p.number, net_class=NetClassName.SIGNAL)
                    c.connect("SWCLK", mcu, swd_clk[0].number)
                elif "RESET" in n and rst:
                    c.connect("MCU_RESET", j, p.number, net_class=NetClassName.SIGNAL)
                    c.connect("MCU_RESET", mcu, rst[0].number)
                elif n.startswith("SWO") and swo:
                    c.connect("SWO", j, p.number, net_class=NetClassName.SIGNAL)
                    c.connect("SWO", mcu, swo[0].number)
                else:
                    c.nc(j, p.number, "unused debug signal")

    # -- user IO -----------------------------------------------------------------
    def _user_io(self) -> None:
        c = self.c
        for led in self.of("led"):
            if not c.free(led, led.pins[0]):
                continue  # charge LED already wired
            if self.gpio is None or self.mcu is None or self.mcu_rail is None:
                self._generic(led, reason="no MCU GPIO to drive the LED")
                continue
            pin = self.gpio.take(f"{led.key}", output=True)
            if pin is None:
                c.finding("SYNTH-GPIO", led.key, Severity.ERROR, "No free GPIO for LED.")
                continue
            tag = f"LED{len([k for k in c.net_members if k.startswith('LED')]) // 2 + 1}"
            c.connect(f"{tag}_DRV", self.mcu, pin.number, net_class=NetClassName.SIGNAL)
            c.res("1k", f"{tag}_DRV", f"{tag}_A", led, "2", "LED series resistor")
            c.connect(f"{tag}_A", led, c.pins(led, func="anode")[0].number)
            c.connect("GND", led, c.pins(led, func="cathode")[0].number)
            vf = led.spec("vf_v") or 2.0
            i = max((self._v(self.mcu_rail) - vf) / 1.0, 0.0)
            led.rail = self.mcu_rail
            # Indicator LEDs: counted at peak, not in the average (on-time is app-specific).
            led.current = CurrentProfile(typ_ma=0.0, max_ma=round(i, 2), sleep_ua=0)
        for btn in self.of("switch"):
            if not c.free(btn, btn.pins[0]):
                continue  # boot / reset button already wired
            if self.gpio is None or self.mcu is None or self.mcu_rail is None:
                self._generic(btn, reason="no MCU GPIO for the button")
                continue
            pin = self.gpio.take(btn.key, wake=True)
            if pin is None:
                c.finding("SYNTH-GPIO", btn.key, Severity.ERROR, "No free GPIO for button.")
                continue
            n = len([k for k in c.net_members if k.startswith("BTN")]) + 1
            net = f"BTN{n}"
            c.connect(net, self.mcu, pin.number, net_class=NetClassName.SIGNAL)
            c.connect(net, btn, "1")
            c.connect("GND", btn, "2")
            c.res("10k", self.mcu_rail, net, btn, "1", "Button pull-up")
            btn.rail = self.mcu_rail

    # -- actuators ---------------------------------------------------------------
    def _rail_in_range(self, part: SPart, prefer_high: bool = False) -> str | None:
        sr = part.lib.supply_range if part.lib else None
        rails = sorted(self.c.rails.values(), key=lambda r: r.nominal_v, reverse=prefer_high)
        for r in rails:
            if sr is None or sr.contains(r.nominal_v):
                return r.name
        return None

    def _actuators(self) -> None:
        c = self.c
        terminals = [t for t in self.of("connector_terminal") if c.free(t, t.pins[0])]
        for n, relay in enumerate(self.of("relay"), start=1):
            pin_map = RELAY_PINOUT.get(relay.mpn)
            if pin_map is None or self.gpio is None or self.mcu is None:
                self._generic(relay, reason="relay pinout/driver not available")
                continue
            rail = self._rail_in_range(relay) or self.mcu_rail or "+3V3"
            relay.rail = rail
            coil_lo = f"RELAY{n}_COIL"
            c.connect(rail, relay, pin_map["coil"][0])
            c.connect(coil_lo, relay, pin_map["coil"][1], net_class=NetClassName.POWER)
            fet = next(
                (
                    m
                    for m in self.of("mosfet")
                    if m.block_id == relay.block_id and c.free(m, m.pins[0])
                ),
                None,
            ) or c.add(
                LOW_SIDE_FET,
                f"Low-side relay driver for {relay.role}",
                key=f"{relay.key}/driver",
                support_for=relay,
                rationale="logic-level N-MOSFET switches the coil from a 3.3 V GPIO",
            )
            diode = next(
                (
                    d
                    for d in self.of("diode")
                    if d.block_id == relay.block_id and c.free(d, d.pins[0])
                ),
                None,
            ) or c.add(
                FLYBACK_DIODE,
                f"Flyback diode across {relay.role} coil",
                key=f"{relay.key}/flyback",
                support_for=relay,
                rationale="clamps the coil's inductive kick when the FET turns off",
            )
            c.connect(coil_lo, fet, c.pins(fet, name="D")[0].number)
            c.connect("GND", fet, c.pins(fet, name="S")[0].number)
            c.connect(rail, diode, c.pins(diode, func="cathode")[0].number)
            c.connect(coil_lo, diode, c.pins(diode, func="anode")[0].number)
            pin = self.gpio.take(f"RELAY{n}", output=True)
            if pin is None:
                c.finding("SYNTH-GPIO", relay.key, Severity.ERROR, "No free GPIO for relay.")
                continue
            drv_net, gate = f"RELAY{n}_DRV", f"RELAY{n}_GATE"
            c.connect(drv_net, self.mcu, pin.number, net_class=NetClassName.SIGNAL)
            c.res("1k", drv_net, gate, fet, "1", "Gate series resistor")
            c.res("100k", gate, "GND", fet, "1", "Gate pull-down (relay off at reset)")
            c.connect(gate, fet, c.pins(fet, name="G")[0].number)
            com, contacts = pin_map["com"][0], pin_map["contacts"]
            c.connect(f"RELAY{n}_COM", relay, com, net_class=NetClassName.POWER)
            for ct in contacts:
                c.connect(f"RELAY{n}_K{ct}", relay, ct, net_class=NetClassName.POWER)
            term = next(
                (t for t in terminals if t.block_id == relay.block_id and len(t.pins) >= 3), None
            )
            if term is not None:
                terminals.remove(term)
                c.connect(f"RELAY{n}_COM", term, term.pins[0].number)
                for tp, ct in zip(term.pins[1:], contacts, strict=False):
                    c.connect(f"RELAY{n}_K{ct}", term, tp.number)
                term.rail = None
            c.notes.append(
                f"{relay.mpn}: contacts on pins {contacts[0]} and {contacts[1]} - confirm which "
                "is normally-open against the manufacturer's datasheet before labelling."
            )
        for n, drv in enumerate(self.of("motor_driver"), start=1):
            if drv.mpn != "DRV8871DDAR" or self.gpio is None or self.mcu is None:
                self._generic(drv, reason="motor driver subcircuit not implemented")
                continue
            vm = self._rail_in_range(drv, prefer_high=True) or next(iter(c.rails))
            drv.rail = vm
            c.ground(drv)
            vm_pin = c.pins(drv, name="VM")[0].number
            c.connect(vm, drv, vm_pin)
            c.cap("10u", vm, drv, vm_pin, "Bulk capacitor")
            c.cap("100n", vm, drv, vm_pin, "Decoupling")
            for name in ("IN1", "IN2"):
                pin = self.gpio.take(f"MOTOR{n}_{name}", output=True)
                if pin is None:
                    c.finding("SYNTH-GPIO", f"{drv.key}.{name}", Severity.ERROR, "No GPIO.")
                    continue
                net = f"MOTOR{n}_{name}"
                c.connect(net, self.mcu, pin.number, net_class=NetClassName.SIGNAL)
                c.connect(net, drv, c.pins(drv, name=name)[0].number)
            ilim = c.pins(drv, name="ILIM")[0].number
            c.res(
                "100k",
                f"MOTOR{n}_ILIM",
                "GND",
                drv,
                ilim,
                "Current-limit resistor",
            )
            c.connect(f"MOTOR{n}_ILIM", drv, ilim, net_class=NetClassName.ANALOG)
            c.notes.append(
                f"{drv.mpn} ILIM = 100 kOhm (about 0.64 A regulation if I_TRIP = 64 kV/R_ILIM "
                "as in the datasheet) - recompute for the actual motor stall current."
            )
            outs = [c.pins(drv, name="OUT1")[0].number, c.pins(drv, name="OUT2")[0].number]
            for i, o in enumerate(outs, start=1):
                c.connect(f"MOTOR{n}_OUT{i}", drv, o, net_class=NetClassName.POWER)
            term = next(
                (t for t in terminals if t.block_id == drv.block_id and len(t.pins) == 2), None
            ) or next((t for t in terminals if len(t.pins) == 2 and self._role(t, "motor")), None)
            if term is not None:
                terminals.remove(term)
                c.connect(f"MOTOR{n}_OUT1", term, term.pins[0].number)
                c.connect(f"MOTOR{n}_OUT2", term, term.pins[1].number)
            cur = self._actuator_current("dc_motor")
            drv.current = CurrentProfile(
                typ_ma=cur, max_ma=cur, sleep_ua=drv.lib.current.sleep_ua if drv.lib else None
            )

    def _actuator_current(self, kind: str) -> float:
        if self.req:
            for a in self.req.actuators:
                if a.kind == kind and a.current_ma:
                    return a.current_ma
        self.c.notes.append(f"No {kind} current in requirements; 500 mA assumed for the budget.")
        return 500.0

    # -- battery sense -----------------------------------------------------------
    def _battery_sense(self) -> None:
        c = self.c
        if (
            "VBAT" not in c.rails
            or c.rails["VBAT"].max_v < 3.5
            or self.mcu is None
            or self.gpio is None
            or self.of("fuel_gauge")
        ):
            return
        pin = self.gpio.take("VBAT_SENSE", adc=True)
        if pin is None:
            c.notes.append("No ADC-capable GPIO left for battery voltage sensing.")
            return
        c.connect("VBAT_SENSE", self.mcu, pin.number, net_class=NetClassName.ANALOG)
        r1 = c.res("1M", "VBAT", "VBAT_SENSE", self.mcu, pin.number, "Battery divider top")
        r2 = c.res("1M", "VBAT_SENSE", "GND", self.mcu, pin.number, "Battery divider bottom")
        c.cap("100n", "VBAT_SENSE", self.mcu, pin.number, "ADC reservoir")
        leak = c.rails["VBAT"].max_v / 2.0  # uA through 2 MOhm
        r1.rail = "VBAT"
        r1.current = CurrentProfile(typ_ma=leak / 1000, max_ma=leak / 1000, sleep_ua=leak)
        del r2
        c.notes.append(
            f"Battery sense divider 1M/1M halves VBAT on {pin.name}; leaks {leak:.1f} uA "
            "continuously."
        )

    # -- headers / connectors ------------------------------------------------------
    def _headers(self) -> None:
        c = self.c
        mcu = self.mcu
        for h in self.of("connector_header"):
            if not c.free(h, h.pins[0]):
                continue
            rail = self.mcu_rail
            if mcu is None or rail is None:
                self._generic(h, reason="no MCU")
                continue
            h.rail = rail
            if c.pins(h, func="i2c_sda") or self._role(h, "i2c", "qwiic", "stemma"):
                if "I2C_SDA" not in c.net_members:
                    self._generic(h, reason="no I2C bus to expose")
                    continue
                named = {p.name: p for p in h.pins}
                order = ["GND", "3V3", "SDA", "SCL"]
                for i, p in enumerate(h.pins[:4]):
                    role = p.name if p.name in named and p.name in order else order[i]
                    net = {"GND": "GND", "3V3": rail, "SDA": "I2C_SDA", "SCL": "I2C_SCL"}[role]
                    c.connect(net, h, p.number)
                for p in h.pins[4:]:
                    c.nc(h, p.number, "mechanical mounting pad")
            elif self._role(h, "uart", "program", "serial", "console", "debug"):
                tx = c.pins(mcu, func="uart_tx")
                rx = c.pins(mcu, func="uart_rx")
                if not tx or not rx:
                    self._generic(h, reason="MCU has no UART pins")
                    continue
                c.connect("GND", h, h.pins[0].number)
                c.connect("UART_TX", h, h.pins[1].number, net_class=NetClassName.SIGNAL)
                c.connect("UART_TX", mcu, tx[0].number)
                c.connect("UART_RX", h, h.pins[2].number, net_class=NetClassName.SIGNAL)
                c.connect("UART_RX", mcu, rx[0].number)
                if len(h.pins) > 3:
                    c.connect(rail, h, h.pins[3].number)
                for p in h.pins[4:]:
                    c.nc(h, p.number, "unused header pin")
                c.notes.append(
                    f"{h.mpn} programming header: 1=GND 2=MCU TX 3=MCU RX 4={rail} (3.3 V logic)."
                )
            else:
                c.connect(rail, h, h.pins[0].number)
                c.connect("GND", h, h.pins[-1].number)
                for i, p in enumerate(h.pins[1:-1], start=1):
                    gp = self.gpio.take(f"EXP{i}") if self.gpio else None
                    if gp is None:
                        c.nc(h, p.number, "no free GPIO")
                        continue
                    c.connect(f"EXP{i}", h, p.number, net_class=NetClassName.SIGNAL)
                    c.connect(f"EXP{i}", mcu, gp.number)
        if mcu is not None and c.pins(mcu, func="uart_tx"):
            for p in c.pins(mcu, func="uart_tx", free_only=True) + c.pins(
                mcu, func="uart_rx", free_only=True
            ):
                c.nc(mcu, p.number, "UART0 console pin not brought out")

    # -- generic fallback ------------------------------------------------------------
    def _generic(self, part: SPart, *, reason: str) -> None:
        c = self.c
        if part.lib is None:
            return
        rail = self._rail_in_range(part) or self.mcu_rail
        c.ground(part)
        pw = [p for p in c.power_pins(part) if c.free(part, p)]
        if pw and rail:
            part.rail = rail
            for p in pw:
                c.connect(rail, part, p.number)
            if part.category in IC_CATEGORIES:
                c.cap("100n", rail, part, pw[0].number, "Decoupling")
        for p in part.pins:
            if c.free(part, p) and (p.type == PinType.NC or "nc" in p.functions):
                c.nc(part, p.number, "NC per KiCad symbol")
        left = [p for p in part.pins if c.free(part, p)]
        for p in left:
            c.nc(part, p.number, f"not synthesized ({reason}) - engineer must complete")
        if left:
            names = ", ".join(f"{p.number}:{p.name or '~'}" for p in left)
            c.finding(
                "SYNTH-INCOMPLETE",
                part.key,
                Severity.WARNING,
                f"{part.mpn} ({part.role}): {reason}. Pins left unconnected: {names}.",
                refs=[part.key],
                fix="Complete this subcircuit by hand and mark the part edited.",
            )

    def _leftover_warning(self, part: SPart) -> None:
        left = [p for p in part.pins if self.c.free(part, p)]
        if left:
            self._generic(part, reason="pins with no synthesis rule")

    def _generic_leftovers(self) -> None:
        c = self.c
        for k in list(c.order):
            part = c.parts[k]
            if part.lib is None or part.origin == "synth":
                continue
            if not part.pins:
                continue
            if any(not c.free(part, p) for p in part.pins):
                # partially wired: only mark remaining NC-type pins
                for p in part.pins:
                    if c.free(part, p) and (p.type == PinType.NC or "nc" in p.functions):
                        c.nc(part, p.number, "NC per KiCad symbol")
                if part is not self.mcu and any(c.free(part, p) for p in part.pins):
                    self._generic(part, reason="pins with no synthesis rule")
                continue
            if part is self.mcu:
                continue
            self._generic(part, reason=f"no synthesis rule for category '{part.category}'")

    def _finalize_mcu(self) -> None:
        c = self.c
        mcu = self.mcu
        if mcu is None:
            return
        for p in mcu.pins:
            if not c.free(mcu, p):
                continue
            if p.type == PinType.NC or "nc" in p.functions:
                c.nc(mcu, p.number, "NC per KiCad symbol")
            elif "strapping" in p.functions or "boot" in p.functions:
                c.nc(mcu, p.number, "strapping pin left at its internal default")
            elif "internal_regulator" in p.functions:
                c.nc(mcu, p.number, "internal regulator node, LDO mode (see module datasheet)")
            elif "crystal" in p.functions:
                c.nc(mcu, p.number, "32 kHz crystal not fitted (internal RC oscillator)")
            elif "usb_dp" in p.functions or "usb_dm" in p.functions or "vbus" in p.functions:
                c.nc(mcu, p.number, "native USB unused")
            elif "gpio" in p.functions:
                c.nc(mcu, p.number, "unused GPIO")
            elif "swd" in p.functions or "reset" in p.functions:
                c.nc(mcu, p.number, "debug pin not brought out")
            else:
                c.nc(mcu, p.number, "unused pin")
        if self.gpio and self.gpio.assigned:
            pairs = ", ".join(f"{s}->{p}" for s, p in self.gpio.assigned.items())
            c.notes.append(f"GPIO assignment on {mcu.mpn}: {pairs}.")

    # -- output ------------------------------------------------------------------
    def _assign_refs(self) -> None:
        used: dict[str, set[int]] = defaultdict(set)
        pending: list[SPart] = []
        for k in self.c.order:
            p = self.c.parts[k]
            prefix = p.lib.ref_prefix if p.lib else "U"
            hint = p.ref_hint or self.previous_refs.get(p.key)
            m = re.match(r"^([A-Z]+)(\d+)$", hint or "")
            if m and m.group(1) == prefix and int(m.group(2)) not in used[prefix]:
                used[prefix].add(int(m.group(2)))
                p.ref = hint or ""
            else:
                pending.append(p)
        for p in pending:
            prefix = p.lib.ref_prefix if p.lib else "U"
            n = 1
            while n in used[prefix]:
                n += 1
            used[prefix].add(n)
            p.ref = f"{prefix}{n}"

    def _finalize(self) -> SynthResult:
        c = self.c
        self._assign_refs()
        ref = {k: p.ref for k, p in c.parts.items()}
        components: list[Component] = []
        for k in c.order:
            p = c.parts[k]
            lp = p.lib
            specs: dict[str, str | int | float | bool] = dict(lp.key_specs) if lp else {}
            specs.update(p.specs)
            components.append(
                Component(
                    ref_des=p.ref,
                    role=p.role,
                    mpn=p.mpn,
                    category=lp.category if lp else None,
                    manufacturer=lp.manufacturer if lp else None,
                    description=lp.description if lp else "unverified part (not in library)",
                    package=lp.package if lp else None,
                    footprint=lp.footprint if lp else None,
                    value=lp.value if lp else None,
                    key_specs=specs,
                    pins=list(lp.pins) if lp else [],
                    supply_range=lp.supply_range if lp else None,
                    current=p.current or (lp.current if lp else CurrentProfile()),
                    alternates=p.alternates,
                    datasheet_url=lp.datasheet_url if lp else None,
                    kicad_symbol=lp.kicad_symbol if lp else None,
                    rationale=p.rationale,
                    confidence=p.confidence,
                    verified=lp is not None,
                    block_id=p.block_id,
                    rail=p.rail,
                    in_bom=lp.in_bom if lp else True,
                    support_for=ref.get(p.support_for) if p.support_for else None,
                    support_pin=p.support_pin,
                    origin=p.origin if p.origin in ("llm", "synth", "human") else "llm",
                    status=p.status,
                    locked=p.locked,
                    key=p.key,
                )
            )
        nets: list[Net] = []
        for name in sorted(c.net_members, key=_net_sort):
            members = c.net_members[name]
            nets.append(
                Net(
                    name=name,
                    net_class=c.net_class.get(name, NetClassName.SIGNAL),
                    connections=[
                        NetConnection(ref=ref[k], pin=pin)
                        for k, pin in sorted(
                            members, key=lambda m: (_natural(ref[m[0]]), _natural(m[1]))
                        )
                    ],
                    voltage_v=c.net_voltage.get(name),
                )
            )
        ncs = [
            NoConnect(ref=ref[k], pin=pin, reason=why)
            for (k, pin), why in sorted(
                c.no_connects.items(), key=lambda kv: (_natural(ref[kv[0][0]]), _natural(kv[0][1]))
            )
        ]
        rails = [
            RailConstraint(
                name=r.name,
                nominal_v=r.nominal_v,
                min_v=r.min_v,
                max_v=r.max_v,
                max_current_ma=r.max_current_ma,
                source_ref=ref.get(r.source_key) if r.source_key else None,
                source_kind=_RAIL_KINDS.get(r.kind, "other"),
                parent_rail=r.parent,
            )
            for r in c.rails.values()
        ]
        findings = []
        for f in c.findings:
            refs = [ref.get(x, x) for x in f.refs]
            findings.append(f.model_copy(update={"refs": refs, "id": _remap_id(f.id, ref)}))
        for u in self.unverified:
            findings.append(
                Finding(
                    id=f"LIB-UNVERIFIED:{u.ref}",
                    rule="LIB-UNVERIFIED",
                    severity=Severity.WARNING,
                    category=FindingCategory.LIBRARY,
                    message=(
                        f"{u.mpn} ({u.role}) is not in the verified library: no pin table, so it "
                        "is excluded from the netlist and its function is missing."
                    ),
                    refs=[u.ref],
                    suggested_fix=(
                        "Pick a library part, or add this MPN to parts_curated.toml with its "
                        "KiCad symbol and rebuild the seed."
                    ),
                )
            )
        return SynthResult(
            components=components,
            netlist=Netlist(nets=nets, no_connects=ncs),
            rails=rails,
            findings=findings,
            notes=c.notes,
        )


_RAIL_KINDS: dict[
    str,
    Literal[
        "usb", "dc_input", "battery", "ldo", "buck", "boost", "charger", "load_switch", "other"
    ],
] = {
    "usb": "usb",
    "dc_input": "dc_input",
    "battery": "battery",
    "ldo": "ldo",
    "buck": "buck",
    "boost": "boost",
    "charger": "charger",
    "load_switch": "load_switch",
}


def _remap_id(fid: str, ref: dict[str, str]) -> str:
    rule, _, subject = fid.partition(":")
    return f"{rule}:{ref.get(subject, subject)}"


def _net_sort(name: str) -> tuple[int, list[object]]:
    if name == "GND":
        return (0, [])
    if name.startswith("+") or name in ("VBUS", "VBAT", "VIN"):
        return (1, _natural(name))
    return (2, _natural(name))


def synthesize(
    lib: ComponentLibrary,
    requirements: Requirements | None,
    selected: list[Selected],
    previous_refs: dict[str, str] | None = None,
) -> SynthResult:
    """Build components + netlist + rails from the selected functional parts."""
    return Synthesizer(lib, requirements, selected, previous_refs).run()


__all__ = [
    "Circuit",
    "Rail",
    "Selected",
    "SynthError",
    "SynthResult",
    "Synthesizer",
    "parse_i2c_addresses",
    "rail_name_for",
    "synthesize",
]
