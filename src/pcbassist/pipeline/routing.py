"""Deterministic routing guidance derived from net classes, rails and the stackup."""

from __future__ import annotations

from ..fab import load_fab_profile
from ..schemas import (
    DesignPlan,
    DiffPair,
    NetClassName,
    NetPriority,
    RoutingGuidance,
    Stackup,
    StackupLayer,
)


def stackup_for(layers: int, thickness: float = 1.6, oz: float = 1.0) -> Stackup:
    cu = 0.035 * oz
    if layers <= 2:
        core = round(thickness - 2 * cu, 3)
        return Stackup(
            layers=[
                StackupLayer(
                    name="F.Cu", kind="copper", thickness_mm=cu, purpose="components, signals"
                ),
                StackupLayer(name="Core", kind="dielectric", thickness_mm=core, purpose="FR-4"),
                StackupLayer(
                    name="B.Cu", kind="copper", thickness_mm=cu, purpose="GND pour, few signals"
                ),
            ],
            total_thickness_mm=thickness,
            copper_weight_oz=oz,
        )
    prepreg = 0.2
    core = round(thickness - 4 * cu - 2 * prepreg, 3)
    return Stackup(
        layers=[
            StackupLayer(
                name="F.Cu", kind="copper", thickness_mm=cu, purpose="components, signals"
            ),
            StackupLayer(name="Prepreg1", kind="dielectric", thickness_mm=prepreg),
            StackupLayer(name="In1.Cu", kind="copper", thickness_mm=cu, purpose="solid GND"),
            StackupLayer(name="Core", kind="dielectric", thickness_mm=core),
            StackupLayer(name="In2.Cu", kind="copper", thickness_mm=cu, purpose="power planes"),
            StackupLayer(name="Prepreg2", kind="dielectric", thickness_mm=prepreg),
            StackupLayer(name="B.Cu", kind="copper", thickness_mm=cu, purpose="signals"),
        ],
        total_thickness_mm=thickness,
        copper_weight_oz=oz,
    )


def build_routing(plan: DesignPlan) -> RoutingGuidance:
    assert plan.constraints is not None and plan.netlist is not None
    cons = plan.constraints
    fab = load_fab_profile(cons.mechanical.fab_profile)
    nets = {n.name: n for n in plan.netlist.nets}
    comps = {c.ref_des: c for c in plan.components}
    pri: list[NetPriority] = []

    def add(target: str, reason: str) -> None:
        pri.append(NetPriority(order=len(pri) + 1, target=target, reason=reason))

    if cons.electrical.rf_keepouts:
        add("antenna keepout", "keep the module antenna area free of copper first")
    if any(n.net_class == NetClassName.HIGH_SPEED for n in nets.values()):
        add("USB_DP/USB_DM", "route the USB pair first: short, parallel, no stubs, over GND")
    switch_nets = [n for n in nets if n.startswith("SW_")]
    if switch_nets:
        add(
            ", ".join(switch_nets),
            "switching node: minimise the loop VIN cap -> regulator -> diode -> GND",
        )
    rails = [r.name for r in cons.electrical.rails]
    if rails:
        add(", ".join(rails), "power rails at Power net-class width; star from the source")
    if cons.electrical.decoupling:
        add("decoupling", "decoupling caps: pad -> cap -> via to GND, shortest possible")
    analog = [n for n, net in nets.items() if net.net_class == NetClassName.ANALOG]
    if analog:
        add(", ".join(analog), "analog/sense nets away from switching nodes and RF")
    add("remaining signals", "Default net class")

    widths = {nc.name: nc.trace_width_mm for nc in cons.electrical.net_classes}
    diff: list[DiffPair] = []
    if "USB_DP" in nets and "USB_DM" in nets:
        diff.append(
            DiffPair(
                name="USB",
                positive="USB_DP",
                negative="USB_DM",
                impedance_ohms=90,
                notes="length-match within 1 mm; ESD array on the pair right at the connector",
            )
        )
    layers = cons.mechanical.layer_count
    if layers <= 2:
        ground = (
            "2-layer: solid GND pour on B.Cu, kept unbroken under the MCU, USB pair and "
            "regulators; GND pour on F.Cu stitched to B.Cu with vias every ~5 mm and around "
            "the board edge. Route signals on F.Cu; use B.Cu only for short jumpers so the "
            "ground plane is not cut."
        )
    else:
        ground = (
            "4-layer: In1.Cu is an unbroken GND plane, In2.Cu carries power pours; route "
            "signals on F.Cu/B.Cu referenced to the adjacent plane."
        )
    ret = [
        "Every signal crossing between zones needs continuous GND underneath it.",
        "Do not route traces through the antenna keepout on any layer.",
    ]
    if switch_nets:
        ret.append("Place the buck input capacitor, catch diode and inductor in one tight loop.")
    motor = [n for n in nets if n.startswith("MOTOR") and "OUT" in n]
    if motor:
        ret.append("Motor output currents return through GND: give the driver its own GND vias.")
    vias = [
        f"Signal vias {fab.min_via_diameter_mm:g}/{fab.min_via_drill_mm:g} mm "
        "(diameter/drill, fab minimum).",
        "Power/GND vias 0.8/0.4 mm; use at least two in parallel per amp of current.",
        "No vias inside SMD pads unless the fab offers filled/capped via-in-pad.",
    ]
    dec_notes = []
    for d in cons.electrical.decoupling:
        host = comps.get(d.ref)
        dec_notes.append(
            f"{', '.join(d.capacitors)} within {d.max_distance_mm:g} mm of "
            f"{d.ref} pin {d.pin}{' (' + host.mpn + ')' if host else ''}"
        )
    notes = []
    for c in plan.components:
        if c.category == "relay":
            notes.append(
                f"{c.ref_des} contacts: keep contact traces >= 2 mm from logic and wide enough for "
                "the switched load (only low-voltage loads are in scope)."
            )
    return RoutingGuidance(
        stackup=stackup_for(layers, cons.mechanical.outline.thickness_mm, fab.copper_oz),
        priorities=pri,
        trace_widths_mm=widths,
        ground_strategy=ground,
        diff_pairs=diff,
        length_matching=["USB_DP/USB_DM: match within 1 mm"] if diff else [],
        return_path_notes=ret,
        via_guidance=vias,
        decoupling_notes=dec_notes,
        notes=notes,
    )


def run_routing_stage(plan: DesignPlan) -> None:
    plan.routing = build_routing(plan)
