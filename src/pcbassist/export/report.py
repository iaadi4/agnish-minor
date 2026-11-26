"""Human-readable design report (``report.md``)."""

from __future__ import annotations

from ..checks.power import BATTERY_DERATING, analyze_power
from ..schemas import DISCLAIMER, DesignPlan, Severity


def _md(s: object) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_md(c) for c in r) + " |" for r in rows]
    return out


def write_report(plan: DesignPlan, files: list[str] | None = None) -> str:
    L: list[str] = []
    title = plan.requirements.summary if plan.requirements else plan.prompt
    L += [f"# Design report: {title}", ""]
    L += [f"> **{DISCLAIMER}**", ""]
    cs = plan.check_summary
    status = cs.status.value if cs else "NOT CHECKED"
    L += [
        f"- **Check status:** `{status}`"
        + (f" ({cs.errors} errors, {cs.warnings} warnings, {cs.infos} info)" if cs else ""),
        f"- **Plan version:** v{plan.version}"
        + (f" (from v{plan.parent_version})" if plan.parent_version else ""),
        f"- **Review status:** {plan.review_status.value}",
        f"- **Created:** {plan.created_at.isoformat(timespec='seconds')}",
    ]
    if plan.llm:
        L.append(
            f"- **LLM:** {plan.llm.mode} / `{plan.llm.model}`"
            + (f" (fixtures: {plan.llm.fixture_set})" if plan.llm.fixture_set else "")
        )
        if plan.llm.mode == "mock":
            L.append("  - Mock mode: the LLM answers are hand-authored fixtures, not model output.")
    if cs:
        L.append(
            f"- **KiCad DRC:** {cs.kicad_drc_note or ('ran' if cs.kicad_drc_ran else 'not run')}"
        )
    L += ["", "## Prompt", "", f"> {_md(plan.prompt)}", ""]
    if plan.feedback_history:
        L += ["Reviewer feedback applied, oldest first:", ""]
        L += [f"{i}. {_md(f)}" for i, f in enumerate(plan.feedback_history, 1)] + [""]

    # -- findings first: they are what a reviewer needs --------------------------
    L += ["## Findings", ""]
    if not plan.findings:
        L += ["No findings.", ""]
    for sev in (Severity.ERROR, Severity.WARNING, Severity.INFO):
        fs = [f for f in plan.findings if f.severity == sev]
        if not fs:
            continue
        L += [f"### {sev.value.upper()} ({len(fs)})", ""]
        L += _table(
            ["ID", "Category", "Message", "Suggested fix"],
            [[f"`{f.id}`", f.category.value, f.message, f.suggested_fix or ""] for f in fs],
        )
        L.append("")

    req = plan.requirements
    if req:
        L += ["## Requirements", ""]
        L += [f"- {_md(g)}" for g in req.functional_goals] + [""]
        if req.power_sources:
            L += _table(
                ["Source", "Nominal V", "Range V", "Capacity mAh", "Max mA", "Notes"],
                [
                    [
                        p.kind,
                        p.nominal_v,
                        f"{p.min_v}-{p.max_v}",
                        p.capacity_mah or "",
                        p.max_current_ma or "",
                        p.notes or "",
                    ]
                    for p in req.power_sources
                ],
            )
            L.append("")
        extra = []
        if req.target_battery_life_days:
            extra.append(f"target battery life {req.target_battery_life_days:g} days")
        if req.duty_cycle:
            extra.append(f"duty cycle: {req.duty_cycle}")
        if req.size.max_width_mm:
            extra.append(f"size <= {req.size.max_width_mm:g} x {req.size.max_height_mm or 0:g} mm")
        if req.wireless:
            extra.append("wireless: " + ", ".join(w.protocol for w in req.wireless))
        if req.sensors:
            extra.append("sensors: " + ", ".join(s.quantity for s in req.sensors))
        if req.actuators:
            extra.append("actuators: " + ", ".join(a.kind for a in req.actuators))
        L += [f"- {_md(e)}" for e in extra] + [""]
        if req.assumptions:
            L += ["### Assumptions", ""]
            L += [
                f"- {_md(a.text)}" + (f" *(fills `{a.field}`)*" if a.field else "")
                for a in req.assumptions
            ]
            L.append("")
        for head, items in (
            ("Open questions", req.open_questions),
            ("Conflicts", req.conflicts),
            ("Out of scope", req.out_of_scope),
        ):
            if items:
                L += [f"### {head}", ""] + [f"- {_md(x)}" for x in items] + [""]

    if plan.architecture:
        L += ["## Architecture", "", "```mermaid", plan.architecture.to_mermaid(), "```", ""]
        L += _table(
            ["Block", "Kind", "Covers"],
            [[b.id, b.kind, ", ".join(b.covers)] for b in plan.architecture.blocks],
        )
        if plan.architecture.rationale:
            L += ["", f"Rationale: {_md(plan.architecture.rationale)}"]
        L.append("")

    if plan.components:
        L += ["## Components", ""]
        L += _table(
            [
                "Ref",
                "Role",
                "MPN",
                "Value",
                "Footprint",
                "Verified",
                "Conf.",
                "Status",
                "Rationale",
            ],
            [
                [
                    c.ref_des + (" (locked)" if c.locked else ""),
                    c.role,
                    c.mpn,
                    c.value or "",
                    (c.footprint or "").split(":")[-1],
                    "yes" if c.verified else "**NO**",
                    f"{c.confidence:.2f}",
                    c.status.value,
                    c.rationale,
                ]
                for c in plan.components
            ],
        )
        L.append("")

    if plan.netlist:
        rep = analyze_power(plan)
        L += ["## Power tree and budget", ""]
        L += _table(
            [
                "Rail",
                "Nominal",
                "Range",
                "Source",
                "From",
                "Capability mA",
                "Peak mA",
                "Active mA",
                "Sleep uA",
            ],
            [
                [
                    b.rail.name,
                    f"{b.rail.nominal_v:g} V",
                    f"{b.rail.min_v:g}-{b.rail.max_v:g} V",
                    f"{b.rail.source_kind} {b.rail.source_ref or ''}",
                    b.rail.parent_rail or "-",
                    b.rail.max_current_ma or "-",
                    f"{b.peak_ma:.1f}",
                    f"{b.typ_ma:.2f}",
                    f"{b.sleep_ua:.1f}",
                ]
                for b in rep.budgets.values()
            ],
        )
        if rep.battery_life_days is not None:
            L += [
                "",
                f"Estimated battery life: **{rep.battery_life_days:.0f} days** "
                f"(average {(rep.avg_current_ma or 0) * 1000:.1f} uA at "
                f"{(rep.duty_fraction or 1) * 100:.3g}% duty, {BATTERY_DERATING:.0%} usable "
                "capacity). Indicator LEDs are counted at peak only.",
            ]
        L.append("")
        L += ["## Netlist", ""]
        L += _table(
            ["Net", "Class", "Pins", "Connections"],
            [
                [
                    n.name,
                    n.net_class.value,
                    len(n.connections),
                    " ".join(f"{c.ref}.{c.pin}" for c in n.connections),
                ]
                for n in plan.netlist.nets
            ],
        )
        if plan.netlist.no_connects:
            L += [
                "",
                f"<details><summary>{len(plan.netlist.no_connects)} explicit no-connects</summary>",
                "",
            ]
            L += [f"- {x.ref}.{x.pin}: {x.reason}" for x in plan.netlist.no_connects]
            L += ["", "</details>"]
        L.append("")

    if plan.constraints:
        e, m = plan.constraints.electrical, plan.constraints.mechanical
        L += ["## Constraints", ""]
        L += [
            f"- Board {m.outline.width_mm:g} x {m.outline.height_mm:g} mm, "
            f"{m.outline.thickness_mm:g} mm thick, "
            f"{m.layer_count} layers, fab profile `{m.fab_profile}`.",
            f"- Trace widths: {e.trace_width_method}.",
            "",
        ]
        L += _table(
            ["Net class", "Width mm", "Clearance mm", "Via mm", "Max mA", "Rationale"],
            [
                [
                    nc.name,
                    nc.trace_width_mm,
                    nc.clearance_mm,
                    f"{nc.via_diameter_mm}/{nc.via_drill_mm}",
                    nc.max_current_ma or "",
                    nc.rationale,
                ]
                for nc in e.net_classes
            ],
        )
        L.append("")
        if e.impedance:
            L += [
                f"- Impedance: {i.target} {i.ohms:g} Ohm {i.kind}. {i.notes}" for i in e.impedance
            ]
        if e.decoupling:
            L += [
                f"- Decoupling: {', '.join(d.capacitors)} within {d.max_distance_mm:g} mm of "
                f"{d.ref} pin {d.pin}"
                for d in e.decoupling
            ]
        for k in e.rf_keepouts + m.keepouts:
            L.append(
                f"- Keepout `{k.name}` ({k.kind}): x {k.rect.x_mm:.1f}-{k.rect.x2:.1f}, "
                f"y {k.rect.y_mm:.1f}-{k.rect.y2:.1f} mm on {', '.join(k.layers)}. {k.reason}"
            )
        for h in m.mounting_holes:
            L.append(
                f"- Mounting hole {h.ref} at ({h.x_mm:g}, {h.y_mm:g}) mm, drill {h.drill_mm:g} mm"
            )
        for cp in m.connectors:
            L.append(f"- {cp.ref} on the {cp.edge.value} edge ({cp.reason})")
        L.append("")

    if plan.placement:
        L += ["## Placement", "", f"Algorithm: {plan.placement.algorithm}.", ""]
        L += _table(
            ["Zone", "Kind", "Area (x, y, w, h mm)", "Purpose"],
            [
                [
                    z.name,
                    z.kind,
                    f"{z.rect.x_mm:.1f}, {z.rect.y_mm:.1f}, {z.rect.w_mm:.1f}, {z.rect.h_mm:.1f}",
                    z.description,
                ]
                for z in plan.placement.zones
            ],
        )
        L.append("")
        L += _table(
            ["Ref", "X mm", "Y mm", "Rot", "Zone", "Hint"],
            [
                [
                    p.ref,
                    f"{p.x_mm:.2f}",
                    f"{p.y_mm:.2f}",
                    f"{p.rotation_deg:g}",
                    p.zone or "",
                    "; ".join(
                        [
                            r.describe(h.ref)
                            for h in plan.placement.hints
                            if h.ref == p.ref
                            for r in h.near
                        ]
                        + [h.notes for h in plan.placement.hints if h.ref == p.ref and h.notes]
                    ),
                ]
                for p in plan.placement.placements
            ],
        )
        L += [""] + [f"- {_md(n)}" for n in plan.placement.notes] + [""]

    if plan.routing:
        r = plan.routing
        L += ["## Routing guidance", ""]
        L += [
            "Stackup: "
            + " / ".join(f"{la.name} {la.thickness_mm:g} mm" for la in r.stackup.layers),
            "",
        ]
        L += [f"{p.order}. **{_md(p.target)}** - {_md(p.reason)}" for p in r.priorities] + [""]
        L += [f"- Ground: {_md(r.ground_strategy)}"]
        L += [
            f"- Diff pair {d.name}: {d.positive}/{d.negative} {d.impedance_ohms:g} Ohm; {d.notes}"
            for d in r.diff_pairs
        ]
        L += [
            f"- {_md(x)}"
            for x in r.length_matching
            + r.return_path_notes
            + r.via_guidance
            + r.decoupling_notes
            + r.notes
        ]
        L.append("")

    if plan.design_notes:
        L += (
            ["## Design notes (deterministic stages)", ""]
            + [f"- {_md(n)}" for n in plan.design_notes]
            + [""]
        )

    L += ["## Review", ""]
    if plan.comments:
        L += _table(
            ["Comment", "Target", "Author", "Text", "Resolved"],
            [
                [c.id, c.target.key(), c.author, c.text, "yes" if c.resolved else "no"]
                for c in plan.comments
            ],
        )
        L.append("")
    if plan.decisions:
        L += _table(
            ["Decision", "Target", "Action", "Author", "Note"],
            [[d.id, d.target.key(), d.decision, d.author, d.note] for d in plan.decisions],
        )
        L.append("")
    if not plan.comments and not plan.decisions:
        L += ["No review comments or decisions yet.", ""]
    if files:
        L += ["## Files", ""] + [f"- `{f}`" for f in files] + [""]
    L += ["---", "", f"*{DISCLAIMER}*", ""]
    return "\n".join(L)
