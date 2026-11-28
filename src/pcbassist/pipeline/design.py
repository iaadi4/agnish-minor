"""The parts and netlist stages: the LLM authors, deterministic code verifies.

``run_parts``   requirements + architecture -> :class:`PartsManifest` (validated against the
                curated library and the KiCad symbol/footprint index)
``run_netlist`` manifest + pin dossier -> :class:`NetlistIntent` (validated for endpoints,
                pin coverage, shorts and rails, then ERC and the power budget)

Both run in the tool loop of :func:`pcbassist.llm.agent.run_validated_stage`: the model can
search the libraries and read real pin tables, and every submission comes back with the
problems to fix until it validates.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..checks.erc import run_erc
from ..checks.power import power_findings
from ..kicad.fpindex import FootprintIndex, default_footprint_index
from ..kicad.symbols import SymbolIndex, default_symbol_index
from ..library import ComponentLibrary
from ..llm.agent import StageResult, Tool, run_validated_stage
from ..llm.base import ToolSpec
from ..schemas import (
    Architecture,
    Component,
    DesignPlan,
    Finding,
    FindingCategory,
    Netlist,
    RailConstraint,
    Requirements,
    Severity,
)
from ..schemas.intent import NetlistIntent, PartsManifest
from . import prompts
from .intent import (
    PASSIVES,
    ManifestResult,
    build_netlist,
    resolve_manifest,
    validate_intent,
)

#: ERROR findings the netlist stage can fix by rewiring (sent back in the netlist loop).
WIRING_RULES = {
    "ERC-UNCONNECTED",
    "ERC-MULTI-NET",
    "ERC-NC-CONNECTED",
    "ERC-UNKNOWN-REF",
    "ERC-UNKNOWN-PIN",
    "ERC-DRIVER-CONFLICT",
    "ERC-POWER-UNDRIVEN",
    "ERC-GROUND-PIN",
    "ERC-SUPPLY-RANGE",
}
#: ERROR findings that need a part added or changed (sent back to the parts stage).
PART_RULE_PREFIXES = ("ERC-", "PWR-")


@dataclass
class Libraries:
    lib: ComponentLibrary
    symbols: SymbolIndex = field(default_factory=default_symbol_index)
    footprints: FootprintIndex = field(default_factory=default_footprint_index)


# ---------------------------------------------------------------------------
# Lookup tools (read-only)
# ---------------------------------------------------------------------------


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, object]:
    return {"type": "object", "properties": props, "required": required}


def lookup_tools(libs: Libraries) -> list[Tool]:
    lib, symbols, fps = libs.lib, libs.symbols, libs.footprints

    def search_parts(a: dict[str, Any]) -> str:
        words = [w for w in str(a.get("query", "")).lower().split() if w]
        scored = []
        for p in lib.all():
            hay = f"{p.mpn} {p.category} {p.description} {' '.join(p.tags)}".lower()
            n = sum(1 for w in words if w in hay)
            if n:
                scored.append((-n, p.category, p.mpn, p))
        scored.sort(key=lambda t: t[:3])
        if not scored:
            return f"no curated part matches {words}; use search_symbols for stock KiCad parts"
        return "\n".join(_lib_line(t[3]) for t in scored[:12])

    def search_symbols(a: dict[str, Any]) -> str:
        q = str(a.get("query", ""))
        hits = symbols.search(q, limit=int(a.get("limit", 8) or 8))
        if not hits:
            return (
                f'no KiCad symbol matches "{q}" in any of {len(symbols.libraries)} libraries; '
                "choose a part whose symbol exists, or a same-family variant"
            )
        return "\n".join(
            f"- {s.lib_id}: {s.description[:90]} | {len(s.pins)} pins"
            + (f" | default footprint {s.footprint}" if s.footprint else "")
            for s in hits
        )

    def symbol_pins(a: dict[str, Any]) -> str:
        lib_id = str(a.get("lib_id", ""))
        r = symbols.resolve(lib_id)
        if r.info is None:
            return r.message(lib_id)
        s = r.info
        multi = f", {s.units} units" if s.units > 1 else ""
        fp = f"\ndefault footprint: {s.footprint}" if s.footprint else ""
        filt = f"\nfootprint filters: {' '.join(s.fp_filters)}" if s.fp_filters else ""
        return f"{s.lib_id} - {len(s.pins)} pins{multi}{fp}{filt}\n{s.pin_table()}"

    def check_footprints(a: dict[str, Any]) -> str:
        ids = a.get("footprints") or []
        out = []
        for fid in ids if isinstance(ids, list) else []:
            fid = str(fid)
            if fid in fps:
                pads = sorted(fps.pad_numbers(fid) or [], key=_nat)
                out.append(f"OK   {fid} ({len(pads)} pads: {' '.join(pads[:30])})")
            else:
                near = fps.nearest(fid)
                out.append(f"MISS {fid}" + (f"; closest: {', '.join(near)}" if near else ""))
        return "\n".join(out) or "pass footprints: a list of 'Lib:Name' ids"

    def search_footprints(a: dict[str, Any]) -> str:
        hits = fps.search(str(a.get("query", "")), limit=12)
        return "\n".join(hits) or "no footprint matches"

    q = {"query": {"type": "string"}}
    return [
        Tool(
            ToolSpec(
                "search_parts",
                "Search the curated, verified component library (MPNs "
                "with verified pins and ratings) by words: category, function, MPN.",
                _obj(q, ["query"]),
            ),
            search_parts,
        ),
        Tool(
            ToolSpec(
                "search_symbols",
                "Search every stock KiCad 8 symbol library for a part "
                "number or description. Returns exact lib_ids. Library names rarely "
                "follow from the part number (INA226 is in Sensor_Energy).",
                _obj({**q, "limit": {"type": "integer"}}, ["query"]),
            ),
            search_symbols,
        ),
        Tool(
            ToolSpec(
                "symbol_pins",
                "The real pins (number=name/type) of a KiCad symbol, its "
                "default footprint and footprint filters. On a miss, where it lives.",
                _obj({"lib_id": {"type": "string"}}, ["lib_id"]),
            ),
            symbol_pins,
        ),
        Tool(
            ToolSpec(
                "check_footprints",
                "Check KiCad footprint ids (Lib:Name) exist and list "
                "their pad numbers; closest installed ids on a miss.",
                _obj(
                    {"footprints": {"type": "array", "items": {"type": "string"}}}, ["footprints"]
                ),
            ),
            check_footprints,
        ),
        Tool(
            ToolSpec(
                "search_footprints",
                "Find KiCad footprint ids whose name or description contains all the given words.",
                _obj(q, ["query"]),
            ),
            search_footprints,
        ),
    ]


def _nat(s: str) -> list[object]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _lib_line(p: Any) -> str:
    sr = f"{p.supply_range.min_v:g}-{p.supply_range.max_v:g} V" if p.supply_range else "-"
    cur = p.current.max_ma if p.current.max_ma is not None else p.current.typ_ma
    specs = json.dumps(p.key_specs, separators=(",", ":"))[:200]
    return (
        f"- {p.mpn} | {p.category} | {p.description} | supply {sr} | "
        f"I {cur if cur is not None else '-'} mA | {p.footprint} | {specs}"
    )


# ---------------------------------------------------------------------------
# Stage 3: parts
# ---------------------------------------------------------------------------


def _catalog(arch: Architecture, lib: ComponentLibrary) -> str:
    from .stages import candidates_for

    cands = candidates_for(arch, lib, per_category=10)
    lines = []
    for b in arch.blocks:
        lines.append(f"## block `{b.id}` ({b.kind}): {b.name}")
        lines += [_lib_line(p) for p in cands.get(b.id, [])] or ["- (no curated candidates)"]
    return "\n".join(lines)


def _passive_table() -> str:
    return "\n".join(f"- {pfx}: {', '.join(sorted(t))}" for pfx, t in PASSIVES.items())


def run_parts(
    ctx: Any,
    req: Requirements,
    arch: Architecture,
    libs: Libraries,
    previous: PartsManifest | None = None,
    locked: list[Component] | None = None,
    rejected: list[Component] | None = None,
) -> tuple[StageResult[PartsManifest], ManifestResult]:
    from .stages import _revision_block

    user = (
        "Requirements:\n"
        + req.model_dump_json(exclude_defaults=True)
        + "\n\nArchitecture:\n"
        + arch.model_dump_json(exclude_defaults=True)
        + "\n\nCurated library candidates per block (verified pins and ratings):\n"
        + _catalog(arch, libs.lib)
        + "\n\nPassive package shorthand (refdes prefix: packages):\n"
        + _passive_table()
    )
    locked_txt = [f"{c.ref_des} {c.mpn} ({c.role})" for c in locked or []]
    rev = _revision_block(ctx, previous.model_dump_json() if previous else None, locked_txt)
    if rejected:
        rev += "\nREJECTED by the reviewer (do not choose again):\n" + "\n".join(
            f"- {c.mpn} ({c.role})" for c in rejected
        )
    if rev:
        user += "\n\n" + rev
    rejected_mpns = {c.mpn for c in rejected or []}
    locked_refs = {c.ref_des: c for c in locked or []}
    holder: dict[str, ManifestResult] = {}

    def validate(m: PartsManifest) -> list[str]:
        r = resolve_manifest(m, arch, libs.lib, libs.symbols, libs.footprints)
        problems = list(r.errors)
        for e in m.parts:
            if e.mpn and e.mpn in rejected_mpns:
                problems.append(f"{e.ref}: {e.mpn} was rejected by the reviewer; choose another")
            lk = locked_refs.get(e.ref)
            if lk is not None and e.mpn != lk.mpn:
                problems.append(f"{e.ref} is LOCKED as {lk.mpn}; keep it")
        for ref, lk in locked_refs.items():
            if ref not in {e.ref for e in m.parts}:
                problems.append(f"{ref} ({lk.mpn}) is LOCKED and must stay in the manifest")
        holder["last"] = r
        return problems

    result = run_validated_stage(
        ctx.client,
        stage="parts",
        output_model=PartsManifest,
        system=prompts.PARTS,
        user=user,
        validate=validate,
        tools=lookup_tools(libs),
        fixture_key=ctx.fixture_key,
        audit=ctx.audit,
    )
    final = resolve_manifest(result.value, arch, libs.lib, libs.symbols, libs.footprints)
    return result, final


# ---------------------------------------------------------------------------
# Stage 4: netlist
# ---------------------------------------------------------------------------


def pin_dossier(comps: list[Component], lib: ComponentLibrary) -> str:
    lines = []
    for c in comps:
        if not c.pins:
            lines.append(f"{c.ref_des} {c.mpn} [off-board] {c.role}: no pins on the board")
            continue
        sr = (
            f" supply {c.supply_range.min_v:g}-{c.supply_range.max_v:g} V" if c.supply_range else ""
        )
        src = "curated" if c.verified else f"KiCad {c.kicad_symbol}"
        sup = f" for {c.support_for}" if c.support_for else ""
        lines.append(
            f"{c.ref_des} {c.value or c.mpn} ({c.mpn}) [{c.category}; {src}] {c.role}{sup}{sr}"
        )
        pins = []
        for p in c.pins:
            tags = [t for t in p.functions if t not in ("power",)]
            nm = "" if p.name in ("~", "") else p.name
            pins.append(f"{p.number}={nm}/{p.type.value}" + (f"[{','.join(tags)}]" if tags else ""))
        lines.append("  pins: " + " ".join(pins))
        lp = lib.get(c.mpn) if c.verified else None
        if lp is not None and lp.notes:
            lines.append(f"  notes: {lp.notes}")
        if c.key_specs:
            lines.append("  specs: " + json.dumps(c.key_specs, separators=(",", ":"))[:300])
    return "\n".join(lines)


def _snapshot(
    req: Requirements, comps: list[Component], netlist: Netlist, rails: list[RailConstraint]
) -> DesignPlan:
    return DesignPlan(
        prompt="(netlist stage check)",
        requirements=req,
        components=comps,
        netlist=netlist,
        power_tree=rails,
    )


def repair_problems(plan: DesignPlan, *, wiring: bool) -> list[str]:
    """ERROR-level ERC/power findings, phrased for the model.

    ``wiring=True``: only what rewiring can fix; ``False``: what needs a part change.
    """
    found: list[Finding] = run_erc(plan) + power_findings(plan)
    out = []
    for f in found:
        if f.severity != Severity.ERROR or not f.rule.startswith(PART_RULE_PREFIXES):
            continue
        if (f.rule in WIRING_RULES) != wiring:
            continue
        fix = f" Fix: {f.suggested_fix}" if f.suggested_fix else ""
        out.append(f"[{f.rule}] {f.message}{fix}")
    return out


@dataclass
class NetlistOutcome:
    result: StageResult[NetlistIntent]
    netlist: Netlist
    rails: list[RailConstraint]
    components: list[Component]
    findings: list[Finding]
    #: problems that need the parts stage (missing decoupling, pull-ups, flyback ...)
    part_problems: list[str] = field(default_factory=list)


def run_netlist(
    ctx: Any,
    req: Requirements,
    arch: Architecture,
    manifest: PartsManifest,
    comps: list[Component],
    libs: Libraries,
    previous: NetlistIntent | None = None,
    locked_nets: list[str] | None = None,
) -> NetlistOutcome:
    from .stages import _revision_block

    user = (
        "Requirements:\n"
        + req.model_dump_json(exclude_defaults=True)
        + "\n\nArchitecture:\n"
        + arch.model_dump_json(exclude_defaults=True)
        + "\n\nPart notes:\n"
        + "\n".join(manifest.notes or ["(none)"])
        + "\n\nPin dossier (real pins of every part; endpoints are REF.PIN with these numbers):\n"
        + pin_dossier(comps, libs.lib)
    )
    rev = _revision_block(ctx, previous.model_dump_json() if previous else None, locked_nets or [])
    if rev:
        user += "\n\n" + rev

    def validate(intent: NetlistIntent) -> list[str]:
        r = validate_intent(intent, comps)
        if r.errors:
            return r.errors
        netlist, rails, annotated = build_netlist(intent, comps)
        return repair_problems(_snapshot(req, annotated, netlist, rails), wiring=True)

    result = run_validated_stage(
        ctx.client,
        stage="netlist",
        output_model=NetlistIntent,
        system=prompts.NETLIST,
        user=user,
        validate=validate,
        tools=lookup_tools(libs)[2:3],  # symbol_pins
        fixture_key=ctx.fixture_key,
        audit=ctx.audit,
    )
    findings: list[Finding] = []
    check = validate_intent(result.value, comps)
    if check.errors:
        # never validated structurally: keep only what can be built, and say so loudly
        for i, msg in enumerate(check.errors, 1):
            findings.append(_netlist_finding(i, msg))
        safe = _drop_bad_endpoints(result.value, comps)
        netlist, rails, annotated = build_netlist(safe, comps)
    else:
        netlist, rails, annotated = build_netlist(result.value, comps)
    # check.warnings (single-endpoint nets) are reported again by ERC; not duplicated here
    part_problems = repair_problems(_snapshot(req, annotated, netlist, rails), wiring=False)
    return NetlistOutcome(result, netlist, rails, annotated, findings, part_problems)


def _netlist_finding(i: int, msg: str, sev: Severity = Severity.ERROR) -> Finding:
    return Finding(
        id=f"NET-INTENT:{i}",
        rule="NET-INTENT",
        severity=sev,
        category=FindingCategory.ERC,
        message=f"Netlist problem the model did not fix: {msg}",
        suggested_fix="Fix the netlist by hand (review edit) or refine the netlist stage.",
    )


def _drop_bad_endpoints(intent: NetlistIntent, comps: list[Component]) -> NetlistIntent:
    by_ref = {c.ref_des: c for c in comps}
    seen: set[str] = set()

    def ok(ep: str) -> bool:
        ref, _, pin = ep.strip().partition(".")
        c = by_ref.get(ref)
        good = c is not None and c.pin(pin) is not None and ep not in seen
        seen.add(ep)
        return good

    nets = []
    names: set[str] = set()
    for n in intent.nets:
        if n.name in names:
            continue
        names.add(n.name)
        pins = [p for p in n.pins if ok(p)]
        if pins:
            nets.append(n.model_copy(update={"pins": pins}))
    nc = [x for x in intent.no_connect if ok(x.pin)]
    rails = [r for r in intent.rails if r.net in names]
    return intent.model_copy(
        update={"nets": nets or intent.nets[:1], "no_connect": nc, "rails": rails}
    )
