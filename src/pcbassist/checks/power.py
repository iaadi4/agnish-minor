"""Power budget: per-rail load currents, regulator limits, thermal, battery life.

Model (documented in PLAN.md):

* Each component draws ``current.typ_ma`` while the design is active, ``current.max_ma``
  (or typ) at peak, and ``current.sleep_ua`` while asleep. Regulators/load switches whose
  sleep current is unknown draw their quiescent current (``typ_ma``) all the time.
* A rail's demand = its direct loads + the input current of every regulator it feeds:
  linear (LDO / load switch): I_in = I_out; switching: I_in = I_out * Vout / (Vin * 0.85).
* A charger draws its programmed charge current from its input rail at peak.
* Battery life = 0.8 * capacity / average current, average = duty-weighted active/sleep.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas import Component, DesignPlan, Finding, FindingCategory, RailConstraint, Severity

SWITCHER_EFFICIENCY = 0.85
BATTERY_DERATING = 0.8
#: Conservative continuous dissipation limits without extra copper (approximate).
PACKAGE_DISSIPATION_W = {
    "SOT-23": 0.25,
    "SOT-23-5": 0.4,
    "SOT-23-6": 0.4,
    "SOT-223": 1.0,
    "TO-220": 1.5,
    "TO-263": 2.0,
}
ALWAYS_ON = {"ldo", "buck", "boost", "load_switch", "charger", "fuel_gauge"}
LOAD_CATEGORIES_NEED_DATA = {
    "mcu_wireless",
    "mcu",
    "sensor_env",
    "sensor_motion",
    "sensor_temp",
    "sensor_light",
    "rtc",
    "usb_uart",
    "motor_driver",
    "relay",
}


@dataclass
class Load:
    ref: str
    typ_ma: float
    peak_ma: float
    sleep_ua: float
    what: str = ""


@dataclass
class RailBudget:
    rail: RailConstraint
    loads: list[Load] = field(default_factory=list)
    typ_ma: float = 0.0
    peak_ma: float = 0.0
    sleep_ua: float = 0.0

    def add(self, load: Load) -> None:
        self.loads.append(load)
        self.typ_ma += load.typ_ma
        self.peak_ma += load.peak_ma
        self.sleep_ua += load.sleep_ua


@dataclass
class PowerReport:
    budgets: dict[str, RailBudget]
    battery_rail: str | None = None
    avg_current_ma: float | None = None
    battery_life_days: float | None = None
    duty_fraction: float | None = None
    no_data: list[str] = field(default_factory=list)


def _load_of(c: Component) -> Load | None:
    cur = c.current
    if cur.typ_ma is None and cur.max_ma is None and cur.sleep_ua is None:
        return None
    typ = cur.typ_ma if cur.typ_ma is not None else (cur.max_ma or 0.0)
    peak = cur.max_ma if cur.max_ma is not None else typ
    if cur.sleep_ua is not None:
        sleep = cur.sleep_ua
    elif c.category in ALWAYS_ON:
        sleep = typ * 1000
    else:
        sleep = typ * 1000  # unknown sleep behaviour: assume it stays on (conservative)
    if c.category == "charger":
        chg = c.key_specs.get("charge_current_ma")
        if isinstance(chg, (int, float)):
            typ += float(chg)
            peak += float(chg)
    return Load(c.ref_des, typ, peak, sleep, c.role)


def rails_of(plan: DesignPlan) -> list[RailConstraint]:
    if plan.constraints is not None and plan.constraints.electrical.rails:
        return plan.constraints.electrical.rails
    return plan.power_tree


def analyze_power(plan: DesignPlan) -> PowerReport:
    rails = rails_of(plan)
    budgets = {r.name: RailBudget(r) for r in rails}
    no_data: list[str] = []
    for c in plan.components:
        if not c.verified or not c.rail or c.rail not in budgets:
            continue
        load = _load_of(c)
        if load is None:
            if c.category in LOAD_CATEGORIES_NEED_DATA:
                no_data.append(c.ref_des)
            continue
        budgets[c.rail].add(load)
    # propagate child rails into parents, deepest first
    depth: dict[str, int] = {}

    def d(name: str, seen: frozenset[str] = frozenset()) -> int:
        if name in depth:
            return depth[name]
        r = budgets[name].rail
        if r.parent_rail is None or r.parent_rail not in budgets or name in seen:
            depth[name] = 0
        else:
            depth[name] = d(r.parent_rail, seen | {name}) + 1
        return depth[name]

    for name in sorted(budgets, key=lambda n: -d(n)):
        b = budgets[name]
        parent = b.rail.parent_rail
        if parent is None or parent not in budgets:
            continue
        pb = budgets[parent]
        if b.rail.source_kind in ("buck", "boost"):
            k = b.rail.nominal_v / (max(pb.rail.nominal_v, 0.1) * SWITCHER_EFFICIENCY)
        else:
            k = 1.0
        pb.add(
            Load(
                ref=b.rail.source_ref or name,
                typ_ma=b.typ_ma * k,
                peak_ma=b.peak_ma * k,
                sleep_ua=b.sleep_ua * k,
                what=f"input of {name}",
            )
        )
    report = PowerReport(budgets=budgets, no_data=no_data)
    batt = next((r for r in rails if r.source_kind == "battery"), None)
    req = plan.requirements
    if batt is not None and req is not None:
        cap = next((p.capacity_mah for p in req.power_sources if p.capacity_mah), None)
        b = budgets[batt.name]
        if req.duty_active_s and req.duty_period_s:
            f = min(req.duty_active_s / req.duty_period_s, 1.0)
        else:
            f = 1.0
        avg = f * b.typ_ma + (1 - f) * b.sleep_ua / 1000
        report.battery_rail = batt.name
        report.duty_fraction = f
        report.avg_current_ma = avg
        if cap and avg > 0:
            report.battery_life_days = BATTERY_DERATING * cap / (avg * 24)
    return report


def _f(
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
        category=FindingCategory.POWER,
        message=msg,
        refs=refs or [],
        nets=nets or [],
        suggested_fix=fix,
    )


def _package_limit(c: Component) -> tuple[str, float] | None:
    fp = (c.footprint or "").split(":")[-1]
    best: tuple[str, float] | None = None
    for pkg, w in PACKAGE_DISSIPATION_W.items():
        if fp.startswith(pkg) and (best is None or len(pkg) > len(best[0])):
            best = (pkg, w)
    return best


def power_findings(plan: DesignPlan, report: PowerReport | None = None) -> list[Finding]:
    report = report or analyze_power(plan)
    out: list[Finding] = []
    comps = {c.ref_des: c for c in plan.components}
    budgets = report.budgets
    for name, b in budgets.items():
        r = b.rail
        src = comps.get(r.source_ref or "")
        cap = r.max_current_ma
        if cap:
            ratio = b.peak_ma / cap
            if r.source_kind in ("usb", "dc_input", "battery"):
                rule = "PWR-SOURCE"
                if ratio > 1.5:
                    sev = Severity.ERROR
                elif ratio > 1.0:
                    sev = Severity.WARNING
                else:
                    sev = None
                if sev is not None:
                    out.append(
                        _f(
                            rule,
                            name,
                            sev,
                            f"{name}: peak demand {b.peak_ma:.1f} mA exceeds what the "
                            f"{r.source_kind} source can deliver ({cap:g} mA).",
                            nets=[name],
                            fix="Use a stronger source, add bulk capacitance for short "
                            "pulses, or reduce the peak load.",
                        )
                    )
            else:
                if ratio > 1.0:
                    out.append(
                        _f(
                            "PWR-REG-CAPACITY",
                            name,
                            Severity.ERROR,
                            f"{name}: peak load {b.peak_ma:.1f} mA exceeds {r.source_ref} "
                            f"rating {cap:g} mA.",
                            refs=[r.source_ref or ""],
                            nets=[name],
                            fix="Choose a regulator with more output current.",
                        )
                    )
                elif ratio > 0.8:
                    out.append(
                        _f(
                            "PWR-REG-CAPACITY",
                            name,
                            Severity.WARNING,
                            f"{name}: peak load {b.peak_ma:.1f} mA is {ratio:.0%} of "
                            f"{r.source_ref} rating {cap:g} mA (little headroom).",
                            refs=[r.source_ref or ""],
                            nets=[name],
                        )
                    )
        parent = budgets.get(r.parent_rail or "")
        if src is not None and parent is not None and r.source_kind in ("ldo", "buck"):
            pr = parent.rail
            drop_mv = src.key_specs.get("dropout_mv")
            dropout = float(drop_mv) / 1000 if isinstance(drop_mv, (int, float)) else 0.3
            if r.source_kind == "buck":
                dropout = max(dropout, 1.5)
            if pr.nominal_v - dropout < r.nominal_v:
                out.append(
                    _f(
                        "PWR-DROPOUT",
                        name,
                        Severity.ERROR,
                        f"{r.source_ref} cannot regulate {name} ({r.nominal_v:g} V) from "
                        f"{pr.name} ({pr.nominal_v:g} V nominal): needs at least "
                        f"{r.nominal_v + dropout:.2f} V including {dropout * 1000:.0f} mV "
                        "dropout.",
                        refs=[r.source_ref or ""],
                        nets=[name, pr.name],
                        fix="Use a buck-boost/boost converter or a higher input voltage.",
                    )
                )
            elif pr.min_v - dropout < r.nominal_v:
                lowest = pr.min_v - dropout
                loads_min = [
                    c.supply_range.min_v
                    for c in plan.components
                    if c.rail == name and c.supply_range is not None
                ]
                need = max(loads_min) if loads_min else r.min_v
                usable_from = need + dropout
                sev = Severity.WARNING
                out.append(
                    _f(
                        "PWR-DROPOUT",
                        name,
                        sev,
                        f"{r.source_ref} drops out below {pr.name} = "
                        f"{r.nominal_v + dropout:.2f} V; at {pr.name} minimum ({pr.min_v:g} V) "
                        f"{name} falls to ~{lowest:.2f} V. Loads need >= {need:g} V, so the "
                        f"source is only usable down to ~{usable_from:.2f} V.",
                        refs=[r.source_ref or ""],
                        nets=[name, pr.name],
                        fix="Accept the reduced usable range or use a buck-boost regulator.",
                    )
                )
            if src.supply_range is not None and not src.supply_range.contains(pr.max_v):
                out.append(
                    _f(
                        "PWR-REG-INPUT",
                        name,
                        Severity.ERROR,
                        f"{r.source_ref} input {pr.name} reaches {pr.max_v:g} V, above its "
                        f"{src.supply_range.max_v:g} V maximum.",
                        refs=[r.source_ref or ""],
                        nets=[pr.name],
                    )
                )
            if r.source_kind == "ldo":
                p_w = (pr.nominal_v - r.nominal_v) * b.typ_ma / 1000
                lim = _package_limit(src)
                if lim is not None:
                    pkg, wmax = lim
                    if p_w > wmax:
                        sev2: Severity | None = Severity.ERROR
                    elif p_w > 0.7 * wmax:
                        sev2 = Severity.WARNING
                    else:
                        sev2 = None
                    if sev2 is not None:
                        out.append(
                            _f(
                                "PWR-THERMAL",
                                str(r.source_ref),
                                sev2,
                                f"{r.source_ref} dissipates ~{p_w:.2f} W "
                                f"(({pr.nominal_v:g}-{r.nominal_v:g}) V x {b.typ_ma:.0f} mA); "
                                f"{pkg} handles ~{wmax:g} W without extra copper.",
                                refs=[r.source_ref or ""],
                                fix="Lower the input voltage, add copper area, or use a buck.",
                            )
                        )
    for ref in report.no_data:
        out.append(
            _f(
                "PWR-NO-DATA",
                ref,
                Severity.WARNING,
                f"{ref} has no current data; it is missing from the budget.",
                refs=[ref],
                fix="Add current figures from the datasheet to the library entry.",
            )
        )
    for c in plan.components:
        if c.category != "charger":
            continue
        chg = c.key_specs.get("charge_current_ma")
        req = plan.requirements
        cap = (
            next((p.capacity_mah for p in req.power_sources if p.capacity_mah), None)
            if req
            else None
        )
        if isinstance(chg, (int, float)) and cap:
            rate = float(chg) / cap
            if rate > 1.0:
                out.append(
                    _f(
                        "PWR-CHARGE-RATE",
                        c.ref_des,
                        Severity.ERROR,
                        f"{c.ref_des} charges at {chg:g} mA = {rate:.2f}C for a {cap:g} mAh cell.",
                        refs=[c.ref_des],
                        fix="Increase R_PROG to lower the charge current to <= 0.5C.",
                    )
                )
            elif rate > 0.7:
                out.append(
                    _f(
                        "PWR-CHARGE-RATE",
                        c.ref_des,
                        Severity.WARNING,
                        f"{c.ref_des} charges at {rate:.2f}C; check the cell's maximum.",
                        refs=[c.ref_des],
                    )
                )
    req = plan.requirements
    if req is not None and req.target_battery_life_days and report.battery_rail:
        life = report.battery_life_days
        target = req.target_battery_life_days
        if life is None:
            out.append(
                _f(
                    "PWR-BATTERY-LIFE",
                    "plan",
                    Severity.WARNING,
                    "Battery life target given but capacity or current data is missing.",
                )
            )
        else:
            avg = report.avg_current_ma or 0.0
            msg = (
                f"Estimated battery life {life:.0f} days (average {avg * 1000:.1f} uA, "
                f"duty {(report.duty_fraction or 1) * 100:.3g}%, {BATTERY_DERATING:.0%} usable "
                f"capacity) vs target {target:g} days."
            )
            if life < target:
                out.append(
                    _f(
                        "PWR-BATTERY-LIFE",
                        "plan",
                        Severity.ERROR,
                        msg,
                        fix="Reduce active time/rate, cut sleep current, or use a bigger battery.",
                    )
                )
            elif life < 1.25 * target:
                out.append(_f("PWR-BATTERY-LIFE", "plan", Severity.WARNING, msg + " Thin margin."))
            else:
                out.append(_f("PWR-BATTERY-LIFE", "plan", Severity.INFO, msg))
    return out
