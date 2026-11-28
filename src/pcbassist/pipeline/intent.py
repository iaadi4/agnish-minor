"""Deterministic validation of what the LLM authors in the parts and netlist stages.

This is the "verification-gated" half of the loop: the model proposes a parts manifest
and a netlist intent; these functions check every claim against the curated library and
the stock KiCad symbol/footprint index and return findings phrased as fixes (naming the
ref, the pin and the closest real alternatives). The model repairs and resubmits until
nothing is left, and only then is the plan built from its output.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from ..kicad.fpindex import FootprintIndex
from ..kicad.symbols import SymbolIndex, SymbolInfo, natural_key
from ..library import ComponentLibrary
from ..schemas import (
    Architecture,
    Component,
    CurrentProfile,
    Finding,
    FindingCategory,
    Net,
    NetClassName,
    NetConnection,
    Netlist,
    NoConnect,
    Pin,
    PinType,
    RailConstraint,
    Severity,
    SupplyRange,
)
from ..schemas.intent import NetlistIntent, PartEntry, PartsManifest

MAX_FEEDBACK = 40

# ---------------------------------------------------------------------------
# Passive shorthand: refdes prefix + package -> stock symbol and footprint
# ---------------------------------------------------------------------------

_METRIC = {"0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216", "1210": "3225"}


def _chip(lib: str, prefix: str, symbol: str) -> dict[str, tuple[str, str]]:
    return {imp: (symbol, f"{lib}:{prefix}_{imp}_{met}Metric") for imp, met in _METRIC.items()}


PASSIVES: dict[str, dict[str, tuple[str, str]]] = {
    "R": _chip("Resistor_SMD", "R", "Device:R"),
    "C": {
        **_chip("Capacitor_SMD", "C", "Device:C"),
        "elec6.3x5.4": ("Device:C_Polarized", "Capacitor_SMD:CP_Elec_6.3x5.4"),
        "radial5mm": ("Device:C_Polarized", "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm"),
    },
    "L": _chip("Inductor_SMD", "L", "Device:L"),
    "FB": _chip("Inductor_SMD", "L", "Device:FerriteBead"),
    "D": {
        "sod-123": ("Device:D", "Diode_SMD:D_SOD-123"),
        "sod-123f": ("Device:D", "Diode_SMD:D_SOD-123F"),
        "sod-323": ("Device:D", "Diode_SMD:D_SOD-323"),
        "sma": ("Device:D", "Diode_SMD:D_SMA"),
        "smb": ("Device:D", "Diode_SMD:D_SMB"),
        "do-41": ("Device:D", "Diode_THT:D_DO-41_SOD81_P10.16mm_Horizontal"),
    },
    "LED": {
        **_chip("LED_SMD", "LED", "Device:LED"),
        "3mm": ("Device:LED", "LED_THT:LED_D3.0mm"),
        "5mm": ("Device:LED", "LED_THT:LED_D5.0mm"),
    },
    "F": _chip("Fuse", "Fuse", "Device:Fuse"),
    "Y": {
        "3225": ("Device:Crystal_GND24", "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm"),
        "5032": ("Device:Crystal", "Crystal:Crystal_SMD_5032-2Pin_5.0x3.2mm"),
        "2012": ("Device:Crystal", "Crystal:Crystal_SMD_2012-2Pin_2.0x1.2mm"),
        "hc49": ("Device:Crystal", "Crystal:Crystal_HC49-U_Vertical"),
    },
    "SW": {
        "6mm": ("Switch:SW_Push", "Button_Switch_THT:SW_PUSH_6mm"),
        "pts645": ("Switch:SW_Push", "Button_Switch_SMD:SW_SPST_PTS645Sx43SMTR92"),
    },
    "TP": {
        "1mm": ("Connector:TestPoint", "TestPoint:TestPoint_Pad_D1.0mm"),
        "1.5mm": ("Connector:TestPoint", "TestPoint:TestPoint_Pad_D1.5mm"),
    },
}
PASSIVE_PREFIX_CATEGORY = {
    "R": "resistor",
    "C": "capacitor",
    "L": "inductor",
    "FB": "ferrite",
    "D": "diode",
    "LED": "led",
    "F": "fuse",
    "Y": "crystal",
    "SW": "switch",
    "TP": "test_point",
}


def ref_prefix(ref: str) -> str:
    m = re.match(r"^[A-Z]+", ref)
    return m.group(0) if m else ""


def _pkg_key(pkg: str) -> str:
    k = pkg.strip().lower().replace(" ", "")
    k = re.sub(r"^(sod|sot|do)[-_]?", r"\1-", k)
    return re.sub(r"(metric|imperial)$", "", k)


def passive_default(prefix: str, package: str) -> tuple[str, str] | None:
    return PASSIVES.get(prefix, {}).get(_pkg_key(package))


def looks_like_description(value: str) -> bool:
    """Prose in a Value field ('P-MOSFET, divider gate') rather than a component value."""
    v = value.strip()
    if len(v) > 24 and " " in v:
        return True
    clauses = [c.strip() for c in v.split(",")]
    return len(clauses) > 1 and any(
        " " in c and re.search(r"[a-z]{3}", c, re.I) for c in clauses[1:]
    )


# ---------------------------------------------------------------------------
# Category inference for parts that are not in the curated library
# ---------------------------------------------------------------------------

_LIB_CATEGORY: list[tuple[str, str]] = [
    (r"^Regulator_Linear$", "ldo"),
    (r"^Battery_Management$", "charger"),
    (r"^Sensor_Motion$", "sensor_motion"),
    (r"^Sensor_Temperature$", "sensor_temp"),
    (r"^Sensor_Optical$", "sensor_light"),
    (r"^Sensor", "sensor_env"),
    (r"^Driver_Motor$", "motor_driver"),
    (r"^Relay", "relay"),
    (r"^Interface_USB$", "usb_uart"),
    (r"^Timer_RTC$", "rtc"),
    (r"^Isolator", "optocoupler"),
    (r"^Transistor_FET$", "mosfet"),
    (r"^Transistor_BJT$", "bjt"),
    (r"^Transistor_Array$", "transistor_array"),
    (r"^Power_Protection$", "esd"),
    (r"^(Amplifier_|Comparator)", "amplifier"),
    (r"^Memory_", "memory"),
    (r"^(Interface|Driver_)", "interface"),
    (r"^Battery$", "battery_holder"),
    (r"^Switch$", "switch"),
    (r"^Mechanical$", "mechanical"),
    (r"^Crystal|^Oscillator$", "crystal"),
    (r"^LED$", "led"),
    (r"^Diode", "diode"),
]


def infer_category(sym: SymbolInfo, ref: str) -> str:
    lib, name, text = sym.lib, sym.name, f"{sym.description} {sym.keywords}".lower()
    if lib == "Device":
        for pat, cat in (
            (r"^R(_|$)", "resistor"),
            (r"^C(_|$)", "capacitor"),
            (r"^L(_|$)", "inductor"),
            (r"^LED", "led"),
            (r"^FerriteBead", "ferrite"),
            (r"^(Fuse|Polyfuse)", "fuse"),
            (r"^Crystal", "crystal"),
            (r"^D(_|$)", "diode"),
            (r"^Q_", "mosfet"),
            (r"^Battery", "battery_holder"),
        ):
            if re.match(pat, name):
                return cat
    if lib.startswith("MCU_") or lib in ("RF_Module", "RF_Bluetooth", "RF_WiFi"):
        wireless = any(w in text for w in ("wifi", "wi-fi", "bluetooth", "ble", "802.15", "lora"))
        return "mcu_wireless" if wireless or lib.startswith("RF_") else "mcu"
    if lib == "Regulator_Switching":
        return "boost" if any(w in text for w in ("boost", "step-up", "step up")) else "buck"
    if lib == "Power_Protection" and "tvs" in text:
        return "tvs"
    if lib == "Power_Management" or "load switch" in text:
        return "load_switch"
    if lib.startswith("Connector"):
        if "usb" in name.lower():
            return "connector_usb"
        if "barrel" in name.lower() or "jack" in name.lower():
            return "connector_power"
        if name.startswith("TestPoint"):
            return "test_point"
        return "connector_header"
    if "fuel gauge" in text:
        return "fuel_gauge"
    for pat, cat in _LIB_CATEGORY:
        if re.match(pat, lib):
            return cat
    if ref_prefix(ref) in PASSIVE_PREFIX_CATEGORY:
        return PASSIVE_PREFIX_CATEGORY[ref_prefix(ref)]
    return "ic" if sym.ref_prefix.startswith("U") else "other"


# ---------------------------------------------------------------------------
# Parts manifest
# ---------------------------------------------------------------------------


@dataclass
class ManifestResult:
    components: list[Component] = field(default_factory=list)
    #: problems the model must fix (fed back in the loop)
    errors: list[str] = field(default_factory=list)
    #: noted, never refused
    warnings: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _pins_from_symbol(sym: SymbolInfo) -> list[Pin]:
    return [
        Pin(number=p.number, name=p.name, type=PinType(p.type), functions=p.functions)
        for p in sorted(sym.pins, key=lambda p: natural_key(p.number))
    ]


def _finding(rule: str, subject: str, sev: Severity, msg: str, **kw: object) -> Finding:
    return Finding(
        id=f"{rule}:{subject}",
        rule=rule,
        severity=sev,
        category=FindingCategory.LIBRARY,
        message=msg,
        **kw,
    )


def resolve_manifest(
    manifest: PartsManifest,
    arch: Architecture,
    lib: ComponentLibrary,
    symbols: SymbolIndex,
    footprints: FootprintIndex,
) -> ManifestResult:
    res = ManifestResult()
    blocks = {b.id for b in arch.blocks}
    seen: set[str] = set()
    refs = {p.ref for p in manifest.parts}

    def err(msg: str) -> None:
        res.errors.append(msg)

    for e in manifest.parts:
        if e.ref in seen:
            err(f"duplicate refdes {e.ref}: one entry per refdes")
            continue
        seen.add(e.ref)
        comp = _resolve_entry(e, lib, symbols, footprints, err, res)
        if e.block_id not in blocks:
            known = ", ".join(sorted(blocks))
            err(f"{e.ref}: block_id '{e.block_id}' is not an architecture block ({known})")
        if e.support_for and e.support_for not in refs:
            err(f"{e.ref}: support_for '{e.support_for}' is not a ref in the manifest")
        if looks_like_description(e.value):
            err(
                f'{e.ref}: "value" reads as a description ("{e.value}"); keep the component '
                'value there ("4.7uF", "STM32F103C8T6") and move the prose to "rationale"'
            )
        if comp is not None:
            res.components.append(comp)

    for s in manifest.substitutions:
        if s.ref not in refs:
            err(f"substitution for {s.ref}: no part entry has that refdes")
    for i, b in enumerate(manifest.blockers, start=1):
        res.findings.append(
            Finding(
                id=f"SEL-BLOCKER:{i}",
                rule="SEL-BLOCKER",
                severity=Severity.ERROR,
                category=FindingCategory.REQUIREMENTS,
                message=f"Requirement cannot be met: {b.constraint} (needs {b.required}); "
                f"best found: {b.found}",
                suggested_fix="Relax the requirement or accept the counter-proposal.",
            )
        )
    return res


def _resolve_entry(
    e: PartEntry,
    lib: ComponentLibrary,
    symbols: SymbolIndex,
    footprints: FootprintIndex,
    add: Callable[[str], None],
    res: ManifestResult,
) -> Component | None:
    prefix = ref_prefix(e.ref)
    curated = lib.get(e.mpn) if e.mpn else None

    base = dict(
        ref_des=e.ref,
        role=e.role,
        value=e.value,
        block_id=e.block_id,
        rationale=e.rationale,
        support_for=e.support_for,
        origin="llm",
        key=f"part:{e.ref}",
    )
    if e.off_board:
        if e.no_part:
            add(f"{e.ref}: off_board and no_part cannot both be true")
        return Component(
            **base,
            mpn=e.mpn or e.value,
            manufacturer=e.manufacturer,
            category="off_board",
            description=e.role,
            confidence=0.5,
        )

    if curated is not None:
        if e.lib_id and curated.kicad_symbol and e.lib_id != curated.kicad_symbol:
            add(
                f"{e.ref}: {curated.mpn} is a curated part with symbol {curated.kicad_symbol}; "
                f'drop "lib_id" or use that one (got {e.lib_id})'
            )
            return None
        if e.footprint and e.footprint != curated.footprint:
            add(
                f"{e.ref}: {curated.mpn} is a curated part with footprint {curated.footprint}; "
                f'drop "footprint" or use that one (got {e.footprint})'
            )
            return None
        current = curated.current
        rt = e.ratings
        if rt is not None and (rt.typ_ma, rt.max_ma, rt.sleep_ua) != (None, None, None):
            current = CurrentProfile(
                typ_ma=rt.typ_ma if rt.typ_ma is not None else curated.current.typ_ma,
                max_ma=rt.max_ma if rt.max_ma is not None else curated.current.max_ma,
                sleep_ua=rt.sleep_ua if rt.sleep_ua is not None else curated.current.sleep_ua,
            )
        return Component(
            **base,
            mpn=curated.mpn,
            category=curated.category,
            manufacturer=curated.manufacturer,
            description=curated.description,
            package=curated.package,
            footprint=curated.footprint,
            key_specs=dict(curated.key_specs),
            pins=list(curated.pins),
            supply_range=curated.supply_range,
            current=current,
            datasheet_url=curated.datasheet_url,
            kicad_symbol=curated.kicad_symbol,
            verified=True,
            pins_verified=True,
            confidence=0.9,
            in_bom=curated.in_bom,
            data_source="library",
        )

    lib_id, footprint = e.lib_id, e.footprint
    if prefix in PASSIVES and e.package and (not lib_id or not footprint):
        d = passive_default(prefix, e.package)
        if d is None:
            add(
                f"{e.ref}: package '{e.package}' is not known for a {prefix} part (known: "
                f"{', '.join(PASSIVES[prefix])}); give lib_id and footprint instead"
            )
            return None
        lib_id, footprint = lib_id or d[0], footprint or d[1]
    passive = prefix in PASSIVE_PREFIX_CATEGORY
    if not lib_id:
        add(
            f'{e.ref}: no "lib_id"; give the exact KiCad symbol (Lib:Name)'
            + (" or a package" if passive else " - search_symbols finds it by part number")
        )
        return None
    if not e.mpn and not e.no_part and not passive:
        add(
            f'{e.ref}: no "mpn"; every active part needs a manufacturer part number '
            '(a board feature with nothing to buy sets "no_part": true)'
        )
    if e.no_part and e.mpn:
        add(f'{e.ref}: "no_part" is true but "mpn" is set; drop one')

    r = symbols.resolve(lib_id)
    if r.info is None:
        add(f"{e.ref}: {r.message(lib_id)}")
        return None
    sym = r.info
    if sym.power:
        add(f"{e.ref}: {lib_id} is a power symbol, not a part; name the net instead")
        return None
    if not footprint:
        hint = f" (the symbol's default is {sym.footprint})" if sym.footprint else ""
        add(f'{e.ref}: no "footprint"; give the exact KiCad footprint id{hint}')
        return None
    if footprint not in footprints:
        near = footprints.nearest(footprint)
        add(
            f"{e.ref}: footprint {footprint} is not in the KiCad libraries"
            + (f"; closest: {', '.join(near)}" if near else "")
        )
        return None
    pads = footprints.pad_numbers(footprint) or set()
    pin_numbers = {p.number for p in sym.pins}
    missing = sorted(pin_numbers - pads, key=natural_key)
    if missing:
        add(
            f"{e.ref}: symbol {lib_id} pins {', '.join(missing[:12])} are not pads of "
            f"{footprint} (pads: {', '.join(sorted(pads, key=natural_key)[:24])}); "
            "choose a matching footprint or symbol"
        )
        return None
    extra = sorted(pads - pin_numbers, key=natural_key)
    if extra and pin_numbers:
        res.warnings.append(
            f"{e.ref}: footprint pads {', '.join(extra[:8])} have no symbol pin "
            "(left unconnected on the board)"
        )

    category = infer_category(sym, e.ref)
    specs: dict[str, str | int | float | bool] = {}
    supply: SupplyRange | None = None
    current = CurrentProfile()
    rt = e.ratings
    if rt is not None:
        lo, hi = rt.supply_min_v, rt.supply_max_v
        if lo is not None and hi is not None and lo <= hi:
            supply = SupplyRange(min_v=rt.supply_min_v, max_v=rt.supply_max_v)
        current = CurrentProfile(typ_ma=rt.typ_ma, max_ma=rt.max_ma, sleep_ua=rt.sleep_ua)
        if rt.output_max_ma is not None:
            specs["iout_max_ma"] = rt.output_max_ma
        if rt.voltage_rating_v is not None:
            specs["voltage_v"] = rt.voltage_rating_v
    if category == "capacitor" and "voltage_v" not in specs:
        m = re.search(r"(\d+(?:\.\d+)?)\s*V\b", e.value)
        if m:
            specs["voltage_v"] = float(m.group(1))
    if not passive and not e.no_part:
        res.findings.append(
            _finding(
                "LIB-KICAD-ONLY",
                e.ref,
                Severity.WARNING,
                f"{e.ref} ({e.mpn or e.value}) is not in the curated library: pins from KiCad "
                f"symbol {lib_id}, footprint {footprint}; MPN, footprint match and ratings are "
                "not verified.",
                refs=[e.ref],
                suggested_fix="Check the MPN, package and ratings against the datasheet.",
            )
        )
    if not passive and rt is not None:
        res.findings.append(
            _finding(
                "LIB-CLAIMED-RATINGS",
                e.ref,
                Severity.INFO,
                f"{e.ref} ({e.mpn or e.value}) is not in the curated library: its pins come from "
                f"the KiCad symbol {lib_id}, its ratings are the model's datasheet claims.",
                refs=[e.ref],
                suggested_fix="Check supply range and currents against the datasheet.",
            )
        )
    return Component(
        **base,
        mpn=e.mpn or e.value,
        category=category,
        manufacturer=e.manufacturer,
        description=sym.description or e.role,
        package=footprint.split(":", 1)[1],
        footprint=footprint,
        key_specs=specs,
        pins=_pins_from_symbol(sym),
        supply_range=supply,
        current=current,
        datasheet_url=sym.datasheet,
        kicad_symbol=lib_id,
        verified=False,
        pins_verified=True,
        confidence=0.7 if passive else 0.6,
        in_bom=not e.no_part,
        data_source="kicad",
    )


# ---------------------------------------------------------------------------
# Netlist intent
# ---------------------------------------------------------------------------

_EP = re.compile(r"^([A-Z]{1,4}[0-9]{1,4})\.(.+)$")


@dataclass
class IntentResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_ground_name(name: str) -> bool:
    return bool(re.match(r"^(GND|AGND|DGND|PGND|VSS|0V)\w*$", name.upper()))


def net_kind(net_name: str, kind: str | None, pins: list[Pin]) -> str:
    if kind:
        return kind
    if _is_ground_name(net_name) or (pins and all("gnd" in p.functions for p in pins)):
        return "ground"
    if any(
        p.type in (PinType.POWER_IN, PinType.POWER_OUT) and "gnd" not in p.functions for p in pins
    ):
        return "power"
    return "signal"


def validate_intent(intent: NetlistIntent, comps: list[Component]) -> IntentResult:
    res = IntentResult()
    by_ref = {c.ref_des: c for c in comps}
    on_board = {c.ref_des: c for c in comps if c.pins}
    used: dict[tuple[str, str], str] = {}
    names: set[str] = set()
    rail_v = {r.net: r.voltage_v for r in intent.rails}

    def parse_ep(ep: str, where: str) -> tuple[str, str] | None:
        m = _EP.match(ep.strip())
        if not m:
            res.errors.append(f'{where}: endpoint "{ep}" is not of the form REF.PIN')
            return None
        ref, pin = m.group(1), m.group(2)
        c = by_ref.get(ref)
        if c is None:
            res.errors.append(f"{where}: {ep} references unknown part {ref}")
            return None
        if not c.pins:
            res.errors.append(f"{where}: {ref} has no pins on the board (off-board part)")
            return None
        if c.pin(pin) is None:
            named = [p for p in c.pins if p.name.upper() == pin.upper()]
            hint = (
                f"; pin name {pin} is pin number {', '.join(p.number for p in named)}"
                if named
                else f"; its pins are {_pin_list(c)}"
            )
            res.errors.append(f"{where}: {ref} has no pin {pin}{hint}")
            return None
        return ref, pin

    for net in intent.nets:
        if net.name in names:
            res.errors.append(f"duplicate net name {net.name}")
        names.add(net.name)
        if not net.pins:
            res.errors.append(f"net {net.name} has no endpoints")
        elif len(net.pins) == 1:
            res.warnings.append(f"net {net.name} has a single endpoint ({net.pins[0]})")
        pins: list[Pin] = []
        for ep in net.pins:
            got = parse_ep(ep, f"net {net.name}")
            if got is None:
                continue
            if got in used:
                res.errors.append(
                    f"pin {got[0]}.{got[1]} is in nets {used[got]} and {net.name} "
                    "(they would be shorted)"
                )
            used[got] = net.name
            p = by_ref[got[0]].pin(got[1])
            if p is not None:
                pins.append(p)
        kind = net_kind(net.name, net.kind, pins)
        feeds_ic = any(
            p.type == PinType.POWER_IN
            and "gnd" not in p.functions
            and by_ref[ep.split(".")[0]].category in RAIL_SINKS
            for ep, p in zip(net.pins, pins, strict=False)
        )
        if kind == "power" and feeds_ic and net.voltage_v is None and net.name not in rail_v:
            res.errors.append(
                f"power net {net.name} has no voltage: set voltage_v or add it to rails"
            )
        if kind != "ground":
            for ep, p in zip(net.pins, pins, strict=False):
                if "gnd" in p.functions and p.type in (PinType.POWER_IN, PinType.PASSIVE):
                    ref = ep.split(".")[0]
                    if by_ref[ref].category not in (
                        "connector_header",
                        "connector_usb",
                        "connector_power",
                    ):
                        res.errors.append(
                            f"{ep} is a ground pin ({p.name}) on {net.name}, not on a ground net"
                        )

    nc: set[tuple[str, str]] = set()
    for x in intent.no_connect:
        got = parse_ep(x.pin, "no_connect")
        if got is None:
            continue
        if got in used:
            res.errors.append(f"{x.pin} is no-connect but is in net {used[got]}")
        p = by_ref[got[0]].pin(got[1])
        if p is not None and p.type == PinType.POWER_IN and "gnd" not in p.functions:
            res.errors.append(f"{x.pin} ({p.name}) is a power input; it must be connected")
        if p is not None and "gnd" in p.functions and p.type == PinType.POWER_IN:
            res.errors.append(f"{x.pin} ({p.name}) is a ground pin; it must be connected")
        nc.add(got)

    for ref, c in on_board.items():
        missing = [
            p
            for p in c.pins
            if (ref, p.number) not in used and (ref, p.number) not in nc and p.type != PinType.NC
        ]
        if missing:
            shown = ", ".join(f"{p.number}({p.name})" for p in missing[:16])
            more = f" and {len(missing) - 16} more" if len(missing) > 16 else ""
            res.errors.append(
                f"{ref}: pins {shown}{more} are in no net and not no-connect; "
                "connect each or list it in no_connect with a reason"
            )

    for r in intent.rails:
        if r.net not in names:
            res.errors.append(f"rail {r.net} is not a net")
        if r.source_ref and r.source_ref not in by_ref:
            res.errors.append(f"rail {r.net}: source_ref {r.source_ref} is not a part")
        if r.parent_net and r.parent_net not in names:
            res.errors.append(f"rail {r.net}: parent_net {r.parent_net} is not a net")
    return res


def _pin_list(c: Component, limit: int = 40) -> str:
    items = [f"{p.number}={p.name}" for p in c.pins[:limit]]
    if len(c.pins) > limit:
        items.append(f"... {len(c.pins) - limit} more")
    return "[" + ", ".join(items) + "]"


_SOURCE_DEFAULT_MA = {"usb": 500.0}

#: Categories whose supply pins make a net a rail that needs a stated voltage.
RAIL_SINKS = {
    "mcu",
    "mcu_wireless",
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
    "usb_uart",
    "load_switch",
    "rtc",
    "amplifier",
    "memory",
    "interface",
    "ic",
}


def _infer_load_rails(
    by_ref: dict[str, Component],
    nets: list[Net],
    kinds: dict[str, str],
    volts: dict[str, float | None],
) -> None:
    """Rail of loads with no power-input pin: relays, LEDs (via their series resistor)."""
    ref_nets: dict[str, list[str]] = defaultdict(list)
    members: dict[str, list[str]] = defaultdict(list)
    for n in nets:
        for cn in n.connections:
            ref_nets[cn.ref].append(n.name)
            members[n.name].append(cn.ref)

    def rail(net: str) -> bool:
        return kinds.get(net) == "power" and volts.get(net) is not None

    for c in by_ref.values():
        has_load = c.current.typ_ma is not None or c.current.max_ma is not None
        if (
            c.rail
            or (c.category in _SUPPORT_CATS - {"led"} and not has_load)
            or c.category in ("off_board", "mechanical", "test_point")
            or (c.category or "").startswith("connector")
        ):
            continue
        direct = [n for n in ref_nets[c.ref_des] if rail(n)]
        if direct:
            c.rail = direct[0]
            continue
        if c.category == "led":
            for nn in ref_nets[c.ref_des]:
                for r in members[nn]:
                    if r != c.ref_des and by_ref[r].category == "resistor":
                        far = [m for m in ref_nets[r] if m != nn and rail(m)]
                        if far:
                            c.rail = far[0]
                            break
                if c.rail:
                    break
    # indicator LEDs and buttons driven from an IC pin: the driving part's rail
    for c in by_ref.values():
        if c.rail or c.category not in ("led", "switch"):
            continue
        near = {r for n in ref_nets[c.ref_des] if kinds.get(n) != "ground" for r in members[n]}
        hop = {
            r2
            for r in near
            if by_ref[r].category == "resistor"
            for n in ref_nets[r]
            if kinds.get(n) != "ground"
            for r2 in members[n]
        }
        for r in sorted(near | hop, key=natural_key):
            host = by_ref[r]
            if host.rail and host.category not in _SUPPORT_CATS and r != c.ref_des:
                c.rail = host.rail
                break


def _charge_currents(
    by_ref: dict[str, Component], pin_net: dict[tuple[str, str], str], kinds: dict[str, str]
) -> None:
    """Programmed charge current from the PROG resistor and the datasheet formula."""
    from ..checks.erc import parse_ohms

    for c in by_ref.values():
        formula = c.key_specs.get("prog_formula")
        if c.category != "charger" or not isinstance(formula, str):
            continue
        m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*R", formula)
        if not m:
            continue
        for p in c.pins:
            if "current_set" not in p.functions:
                continue
            net = pin_net.get((c.ref_des, p.number))
            for r in by_ref.values():
                if r.category != "resistor":
                    continue
                nets = [pin_net.get((r.ref_des, q.number)) for q in r.pins]
                if net in nets and any(kinds.get(n or "") == "ground" for n in nets):
                    ohms = parse_ohms(r.value)
                    if ohms:
                        i_ma = round(float(m.group(1)) / (ohms / 1000), 1)
                        c.key_specs = {**c.key_specs, "charge_current_ma": i_ma}


def _assign_i2c_addresses(
    by_ref: dict[str, Component], pin_net: dict[tuple[str, str], str], kinds: dict[str, str]
) -> None:
    """Resolve each I2C part's address from its address-pin straps (for duplicate checks)."""
    from .synth import parse_i2c_addresses

    for c in by_ref.values():
        addrs = parse_i2c_addresses(c.key_specs.get("i2c_address"))
        if not addrs or not any("i2c_sda" in p.functions for p in c.pins):
            continue
        idx = 0
        known = True
        for bit, p in enumerate(q for q in c.pins if "i2c_addr" in q.functions):
            net = pin_net.get((c.ref_des, p.number))
            if net is None:
                known = False
            elif kinds.get(net) == "power":
                idx |= 1 << bit
            elif kinds.get(net) != "ground":
                known = False
        if known:
            c.key_specs = {
                **c.key_specs,
                "i2c_address_assigned": f"0x{addrs[min(idx, len(addrs) - 1)]:02X}",
            }


def build_netlist(
    intent: NetlistIntent, comps: list[Component]
) -> tuple[Netlist, list[RailConstraint], list[Component]]:
    """Turn a validated intent into the plan's netlist, rails and annotated components."""
    by_ref = {c.ref_des: c.model_copy(deep=True) for c in comps}
    rail_by = {r.net: r for r in intent.rails}
    nets: list[Net] = []
    kinds: dict[str, str] = {}
    for n in intent.nets:
        conns = []
        pins: list[Pin] = []
        for ep in n.pins:
            ref, pin = ep.strip().split(".", 1)
            conns.append(NetConnection(ref=ref, pin=pin))
            p = by_ref[ref].pin(pin)
            if p is not None:
                pins.append(p)
        kind = net_kind(n.name, n.kind, pins)
        kinds[n.name] = kind
        if kind == "ground":
            cls = NetClassName.GROUND
        elif kind == "power":
            cls = NetClassName.POWER
        else:
            cls = n.net_class or NetClassName.SIGNAL
        v = (
            n.voltage_v
            if n.voltage_v is not None
            else (rail_by[n.name].voltage_v if n.name in rail_by else None)
        )
        nets.append(
            Net(
                name=n.name,
                net_class=cls,
                connections=conns,
                voltage_v=v if kind == "power" else (0.0 if kind == "ground" else None),
                notes=n.notes,
            )
        )
    no_connects = []
    for x in intent.no_connect:
        ref, pin = x.pin.strip().split(".", 1)
        no_connects.append(NoConnect(ref=ref, pin=pin, reason=x.reason))
    for c in by_ref.values():
        named = {(nc.ref, nc.pin) for nc in no_connects}
        conn = {(cn.ref, cn.pin) for n in nets for cn in n.connections}
        for p in c.pins:
            if p.type == PinType.NC and (c.ref_des, p.number) not in named | conn:
                no_connects.append(
                    NoConnect(ref=c.ref_des, pin=p.number, reason="NC pin per the KiCad symbol")
                )
    netlist = Netlist(nets=nets, no_connects=no_connects)

    pin_net = {(cn.ref, cn.pin): n.name for n in nets for cn in n.connections}
    # supply rail of each part: the net on its first non-ground power input
    for c in by_ref.values():
        for p in c.pins:
            if p.type == PinType.POWER_IN and "gnd" not in p.functions:
                net = pin_net.get((c.ref_des, p.number))
                if net and kinds.get(net) == "power":
                    c.rail = net
                    break
    volts = {n.name: n.voltage_v for n in nets}
    _infer_load_rails(by_ref, nets, kinds, volts)
    _infer_support(by_ref, nets, kinds)
    _assign_i2c_addresses(by_ref, pin_net, kinds)
    _charge_currents(by_ref, pin_net, kinds)

    rails: list[RailConstraint] = []
    for rnet in nets:
        if kinds[rnet.name] != "power" or rnet.voltage_v is None:
            continue
        r = rail_by.get(rnet.name)
        v = rnet.voltage_v or 0.0
        src = by_ref.get(r.source_ref) if r and r.source_ref else None
        cap = r.max_current_ma if r else None
        if cap is None and src is not None:
            iout = src.key_specs.get("iout_max_ma")
            if isinstance(iout, (int, float)) and not isinstance(iout, bool):
                cap = float(iout)
        kind = r.source_kind if r else "other"
        if cap is None:
            cap = _SOURCE_DEFAULT_MA.get(kind)
        rails.append(
            RailConstraint(
                name=rnet.name,
                nominal_v=v,
                min_v=r.min_v if r and r.min_v is not None else round(v * 0.95, 3),
                max_v=r.max_v if r and r.max_v is not None else round(v * 1.05, 3),
                max_current_ma=cap,
                source_ref=r.source_ref if r else None,
                source_kind=kind,
                parent_rail=r.parent_net if r else None,
            )
        )
    return netlist, rails, list(by_ref.values())


_SUPPORT_CATS = {
    "resistor",
    "capacitor",
    "inductor",
    "ferrite",
    "led",
    "diode",
    "crystal",
    "fuse",
    "tvs",
    "esd",
}


def _infer_support(by_ref: dict[str, Component], nets: list[Net], kinds: dict[str, str]) -> None:
    """Fill support_for/support_pin for passives the model did not annotate."""
    members: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for n in nets:
        for cn in n.connections:
            members[n.name].append((cn.ref, cn.pin))
    ref_nets: dict[str, list[str]] = defaultdict(list)
    for n in nets:
        for cn in n.connections:
            ref_nets[cn.ref].append(n.name)
    hosts = {r for r, c in by_ref.items() if c.category not in _SUPPORT_CATS | {"off_board"}}
    for ref, c in by_ref.items():
        if c.category not in _SUPPORT_CATS:
            continue
        if c.support_for and c.support_for in by_ref:
            host = by_ref[c.support_for]
            if c.support_pin is None:
                for net in ref_nets[ref]:
                    pin = next((p for r2, p in members[net] if r2 == host.ref_des), None)
                    if pin and kinds.get(net) != "ground":
                        c.support_pin = pin
                        break
            continue
        best: tuple[int, str, str] | None = None
        for net in ref_nets[ref]:
            if kinds.get(net) == "ground":
                continue
            weight = 2 if kinds.get(net) == "signal" else 1
            for r2, pin in members[net]:
                if r2 in hosts and r2 != ref:
                    # an IC beats a connector as the part a support component serves
                    ic = 0 if (by_ref[r2].category or "").startswith("connector") else 1
                    cand = (weight * 2 + ic, r2, pin)
                    if (
                        best is None
                        or cand[0] > best[0]
                        or (cand[0] == best[0] and natural_key(r2) < natural_key(best[1]))
                    ):
                        best = cand
        if best is not None:
            c.support_for, c.support_pin = best[1], best[2]


def cap_feedback(items: list[str]) -> str:
    shown = items[:MAX_FEEDBACK]
    out = [f"{len(items)} problem(s) to fix:"] + [f"  {i}. {m}" for i, m in enumerate(shown, 1)]
    if len(items) > MAX_FEEDBACK:
        out.append(f"  ... and {len(items) - MAX_FEEDBACK} more; fix these and resubmit")
    return "\n".join(out)
