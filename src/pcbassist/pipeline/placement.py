"""Deterministic placement: zones, edge items, greedy nearest-free-spot packing.

Algorithm (all coordinates in mm, board origin top-left, +y down):

1. Mounting holes at the corner positions chosen by the constraints stage.
2. Edge items (connectors, PCB-antenna modules) flush against their assigned edge,
   centred as a group along that edge.
3. Every other functional part is assigned a zone from its architecture block kind; each
   zone has a preferred point (power near the power input, sensors far from it, drivers
   near the output terminals). The part goes to the free position nearest that point.
4. Support parts (decoupling, pull-ups, ...) go to the free position nearest the pad of the
   pin they serve, so decouplers end up next to their pin.

"Free" means the courtyard (+0.25 mm gap) is inside the board minus the fab's
component-to-edge distance and overlaps no placed courtyard. Positions are on a 0.5 mm
grid. The result is a starting point for a human layout, not a finished layout.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..fab import load_fab_profile
from ..kicad.footprint import (
    FootprintGeom,
    load_footprint,
    pad_position,
    placed_bbox,
    placed_rects,
)
from ..schemas import (
    Component,
    ComponentPlacement,
    DesignPlan,
    Edge,
    Finding,
    FindingCategory,
    Keepout,
    PlacementHint,
    PlacementPlan,
    ProximityRule,
    Rect,
    Severity,
    Side,
    Zone,
)

GRID = 0.5
GAP = 0.25
HALO = 2.0
EDGE_GAP = 1.5

ZONE_OF_BLOCK = {
    "power_input": "power",
    "charging": "power",
    "battery": "power",
    "regulation": "power",
    "protection": "power",
    "mcu_wireless": "mcu",
    "mcu": "mcu",
    "clock": "mcu",
    "sensor": "sensor",
    "actuator_driver": "driver",
    "isolation": "driver",
    "debug_programming": "debug",
    "user_io": "general",
    "connector": "connector",
    "other": "general",
}
ZONE_ORDER = ["power", "mcu", "driver", "sensor", "debug", "general", "connector"]
ZONE_POINT = {
    "power": (0.2, 0.55),
    "mcu": (0.5, 0.45),
    "driver": (0.8, 0.5),
    "sensor": (0.88, 0.88),
    "debug": (0.5, 0.92),
    "general": (0.6, 0.85),
    "connector": (0.5, 0.5),
}
ZONE_TEXT = {
    "power": "Power entry, protection, charging and regulation; keep high-current loops short.",
    "mcu": "MCU and its local decoupling/strapping.",
    "rf": "Antenna keepout: no copper, components or vias on any layer.",
    "driver": "Actuator drivers next to their output terminals; wide copper for load current.",
    "sensor": "Sensors away from regulators and the MCU module to limit self-heating.",
    "debug": "Programming/debug access near a board edge.",
    "general": "User I/O and miscellaneous parts.",
    "connector": "Edge connectors.",
}


@dataclass
class Placed:
    comp: Component
    geom: FootprintGeom
    x: float
    y: float
    rot: float
    zone: str
    edge: Edge | None = None

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return placed_bbox(self.geom, self.x, self.y, self.rot)

    @property
    def rects(self) -> list[tuple[float, float, float, float]]:
        return placed_rects(self.geom, self.x, self.y, self.rot)


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _inflate(r: tuple[float, float, float, float], d: float) -> tuple[float, float, float, float]:
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


class Placer:
    def __init__(self, plan: DesignPlan) -> None:
        assert plan.constraints is not None
        self.plan = plan
        mech = plan.constraints.mechanical
        self.w = mech.outline.width_mm
        self.h = mech.outline.height_mm
        fab = load_fab_profile(mech.fab_profile)
        self.margin = fab.component_to_edge_mm
        self.placed: dict[str, Placed] = {}
        self.blocked: list[tuple[float, float, float, float]] = []
        #: space reserved around ICs for their support parts while big parts are placed
        self.halos: list[tuple[float, float, float, float]] = []
        self.use_halos = False
        self.findings: list[Finding] = []
        kinds = {b.id: b.kind for b in plan.architecture.blocks} if plan.architecture else {}
        self.zone: dict[str, str] = {}
        by_ref = {c.ref_des: c for c in plan.components}
        for c in plan.components:
            anchor = by_ref.get(c.support_for or "")
            block = (anchor or c).block_id
            self.zone[c.ref_des] = ZONE_OF_BLOCK.get(kinds.get(block or "", "other"), "general")

    # -- geometry helpers -------------------------------------------------------
    def fits(
        self,
        rects: list[tuple[float, float, float, float]],
        margin: float | None = None,
    ) -> bool:
        m = self.margin if margin is None else margin
        for bbox in rects:
            if bbox[0] < m - 1e-9 or bbox[1] < m - 1e-9:
                return False
            if bbox[2] > self.w - m + 1e-9 or bbox[3] > self.h - m + 1e-9:
                return False
            probe = _inflate(bbox, GAP - 1e-9)
            if any(_overlap(probe, o) for o in self.blocked):
                return False
            if self.use_halos and any(_overlap(probe, o) for o in self.halos):
                return False
        return True

    def commit(self, p: Placed) -> None:
        self.placed[p.comp.ref_des] = p
        self.blocked.extend(p.rects)

    def find_spot(
        self, geom: FootprintGeom, target: tuple[float, float], rot: float = 0.0
    ) -> tuple[float, float] | None:
        """Free origin position whose courtyard centre is nearest ``target``."""
        x0, y0, x1, y1 = placed_bbox(geom, 0, 0, rot)
        local = placed_rects(geom, 0, 0, rot)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        ox = round((target[0] - cx) / GRID) * GRID
        oy = round((target[1] - cy) / GRID) * GRID
        max_r = int(max(self.w, self.h) / GRID) + 2
        for r in range(max_r + 1):
            best: tuple[float, float, float] | None = None
            for i in range(-r, r + 1):
                for j in (-r, r) if abs(i) != r else range(-r, r + 1):
                    x, y = ox + i * GRID, oy + j * GRID
                    rs = [(x + a, y + b, x + c, y + d) for a, b, c, d in local]
                    if not self.fits(rs):
                        continue
                    d = (x + cx - target[0]) ** 2 + (y + cy - target[1]) ** 2
                    if best is None or d < best[0]:
                        best = (d, x, y)
            if best is not None:
                return (round(best[1], 3), round(best[2], 3))
        return None

    # -- stages -------------------------------------------------------------------
    def holes(self) -> None:
        assert self.plan.constraints is not None
        by_ref = {c.ref_des: c for c in self.plan.components}
        for hole in self.plan.constraints.mechanical.mounting_holes:
            c = by_ref.get(hole.ref)
            geom = load_footprint(c.footprint) if c and c.footprint else None
            if c is None or geom is None:
                continue
            p = Placed(c, geom, hole.x_mm, hole.y_mm, 0.0, "general")
            self.commit(p)

    def edges(self) -> None:
        assert self.plan.constraints is not None
        by_ref = {c.ref_des: c for c in self.plan.components}
        groups: dict[Edge, list[Component]] = {}
        for cp in self.plan.constraints.mechanical.connectors:
            c = by_ref.get(cp.ref)
            if c is not None and c.ref_des not in self.placed:
                groups.setdefault(cp.edge, []).append(c)
        order = sorted(groups, key=lambda e: 0 if e == Edge.TOP else 1)
        for edge in order:
            comps = groups[edge]
            geoms = [(c, load_footprint(c.footprint or "")) for c in comps]
            items = [(c, g) for c, g in geoms if g is not None]
            horizontal = edge in (Edge.TOP, Edge.BOTTOM)
            span = self.w if horizontal else self.h
            sizes = [(g.width if horizontal else g.height) for _, g in items]
            total = sum(sizes) + EDGE_GAP * (len(sizes) - 1)
            # Antenna modules (top edge) start from the right corner so the left side stays
            # free for the power zone; other edges centre their group.
            cursor = max(span - total, 0.0) if edge == Edge.TOP else max((span - total) / 2, 0.0)
            for (c, g), size in zip(items, sizes, strict=True):
                x0, y0, x1, y1 = g.courtyard
                done = False
                for shift in [k * GRID for k in range(int(span / GRID) + 1)]:
                    for sign in (1, -1):
                        pos = cursor + sign * shift
                        if horizontal:
                            x = pos - x0
                            y = -y0 if edge == Edge.TOP else self.h - y1
                        else:
                            y = pos - y0
                            x = -x0 if edge == Edge.LEFT else self.w - x1
                        x, y = round(x / GRID) * GRID, round(y / GRID) * GRID
                        if horizontal:
                            y = -y0 if edge == Edge.TOP else self.h - y1
                        else:
                            x = -x0 if edge == Edge.LEFT else self.w - x1
                        if self.fits(placed_rects(g, x, y), margin=0.0):
                            self.commit(
                                Placed(c, g, round(x, 3), round(y, 3), 0.0, "connector", edge)
                            )
                            done = True
                            break
                    if done:
                        break
                if not done:
                    self.findings.append(
                        Finding(
                            id=f"PLACE-EDGE:{c.ref_des}",
                            rule="PLACE-EDGE",
                            severity=Severity.WARNING,
                            category=FindingCategory.PLACEMENT,
                            message=f"{c.ref_des} could not be placed on the {edge.value} edge.",
                            refs=[c.ref_des],
                        )
                    )
                cursor += size + EDGE_GAP
            for c, maybe in geoms:
                if maybe is not None and c.ref_des in self.placed and c.category == "mcu_wireless":
                    self.placed[c.ref_des].zone = "mcu"

    def anchors(self) -> None:
        """Functional parts, largest first, each at its zone's preferred point."""
        comps = [
            c
            for c in self.plan.components
            if c.pins_known and c.ref_des not in self.placed and not c.support_for
        ]
        comps.sort(
            key=lambda c: (
                -self._area(c),
                ZONE_ORDER.index(self.zone.get(c.ref_des, "general")),
                c.ref_des,
            )
        )
        has_support = {c.support_for for c in self.plan.components if c.support_for}
        power_in = next(
            (p for p in self.placed.values() if p.edge == Edge.LEFT and p.zone == "connector"),
            None,
        )
        for p in self.placed.values():
            if p.comp.ref_des in has_support:
                self.halos += [_inflate(r, HALO) for r in p.rects]
        self.use_halos = True
        for c in comps:
            zone = self.zone.get(c.ref_des, "general")
            fx, fy = ZONE_POINT.get(zone, (0.5, 0.5))
            target = (fx * self.w, fy * self.h)
            if zone == "power" and power_in is not None:
                b = power_in.bbox
                target = (b[2] + 6.0, (b[1] + b[3]) / 2)
            self._place_near(c, target, zone)
            if c.ref_des in self.placed and c.ref_des in has_support:
                self.halos += [_inflate(r, HALO) for r in self.placed[c.ref_des].rects]
        self.use_halos = False
        # support parts after all functional parts, in placement order
        for p in list(self.placed.values()):
            self.supports(p.comp.ref_des)

    def supports(self, anchor_ref: str) -> None:
        anchor = self.placed.get(anchor_ref)
        sups = [
            c
            for c in self.plan.components
            if c.support_for == anchor_ref and c.pins_known and c.ref_des not in self.placed
        ]
        sups.sort(key=lambda c: (0 if c.role.startswith("Decoupling") else 1, c.ref_des))
        for c in sups:
            target: tuple[float, float]
            if anchor is None:
                target = (self.w / 2, self.h / 2)
            else:
                pos = (
                    pad_position(anchor.geom, c.support_pin, anchor.x, anchor.y, anchor.rot)
                    if c.support_pin
                    else None
                )
                if pos is None:
                    b = anchor.bbox
                    pos = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
                target = pos
            self._place_near(c, target, self.zone.get(c.ref_des, "general"))
            self.supports(c.ref_des)

    def leftovers(self) -> None:
        for c in self.plan.components:
            if c.pins_known and c.ref_des not in self.placed and c.footprint:
                self._place_near(c, (self.w / 2, self.h / 2), self.zone.get(c.ref_des, "general"))

    def _area(self, c: Component) -> float:
        g = load_footprint(c.footprint) if c.footprint else None
        return g.width * g.height if g else 0.0

    def _place_near(self, c: Component, target: tuple[float, float], zone: str) -> None:
        if c.ref_des in self.placed:
            return
        g = load_footprint(c.footprint) if c.footprint else None
        if g is None:
            self.findings.append(
                Finding(
                    id=f"PLACE-NO-FOOTPRINT:{c.ref_des}",
                    rule="PLACE-NO-FOOTPRINT",
                    severity=Severity.WARNING,
                    category=FindingCategory.PLACEMENT,
                    message=f"{c.ref_des}: footprint {c.footprint} geometry not available.",
                    refs=[c.ref_des],
                )
            )
            return
        spot = self.find_spot(g, target)
        if spot is None:
            self.findings.append(
                Finding(
                    id=f"PLACE-NO-SPACE:{c.ref_des}",
                    rule="PLACE-NO-SPACE",
                    severity=Severity.ERROR,
                    category=FindingCategory.PLACEMENT,
                    message=f"{c.ref_des} ({c.mpn}) does not fit on the "
                    f"{self.w:g} x {self.h:g} mm board.",
                    refs=[c.ref_des],
                    suggested_fix="Enlarge the board or remove parts.",
                )
            )
            return
        self.commit(Placed(c, g, spot[0], spot[1], 0.0, zone))

    # -- outputs ------------------------------------------------------------------
    def rf_keepouts(self) -> list[Keepout]:
        out: list[Keepout] = []
        for p in self.placed.values():
            c = p.comp
            ant = str(c.key_specs.get("antenna", "")).lower()
            if c.category != "mcu_wireless" or not ("pcb" in ant or "trace" in ant):
                continue  # chip antennas: the module's own keepout applies
            x0, y0, x1, y1 = p.bbox
            pads_top = min((pp.y - pp.h / 2 for pp in p.geom.pads), default=y1)
            ant_bottom = p.y + pads_top - 0.5
            if ant_bottom - y0 < 1.0:
                continue
            ko_top = 0.0 if y0 < 3.0 else y0
            rect = Rect(x_mm=x0, y_mm=ko_top, w_mm=x1 - x0, h_mm=ant_bottom - ko_top)
            out.append(
                Keepout(
                    name=f"{c.ref_des}_antenna",
                    kind="rf",
                    rect=rect,
                    layers=["F.Cu", "B.Cu"],
                    reason=(
                        "PCB antenna region of the module (from its footprint courtyard, above "
                        "the pads) plus the strip to the board edge: no copper on any layer. "
                        "Confirm against the module datasheet's keepout drawing."
                    ),
                )
            )
            if y0 > 1.0:
                self.findings.append(
                    Finding(
                        id=f"PLACE-ANTENNA:{c.ref_des}",
                        rule="PLACE-ANTENNA",
                        severity=Severity.WARNING,
                        category=FindingCategory.PLACEMENT,
                        message=f"{c.ref_des} antenna is {y0:.1f} mm from the board edge; "
                        "it should sit at (or overhang) the edge.",
                        refs=[c.ref_des],
                    )
                )
        return out

    def hints(self) -> list[PlacementHint]:
        assert self.plan.constraints is not None
        out: list[PlacementHint] = []
        dec = {
            (d.ref, cap): d.max_distance_mm
            for d in self.plan.constraints.electrical.decoupling
            for cap in d.capacitors
        }
        edges = {cp.ref: cp for cp in self.plan.constraints.mechanical.connectors}
        for c in self.plan.components:
            if not c.pins_known:
                continue
            near: list[ProximityRule] = []
            if c.support_for:
                dist = dec.get((c.support_for, c.ref_des))
                near.append(
                    ProximityRule(
                        target_ref=c.support_for,
                        target_pin=c.support_pin,
                        max_distance_mm=dist if dist else 10.0,
                        reason=c.role,
                    )
                )
            cp = edges.get(c.ref_des)
            out.append(
                PlacementHint(
                    ref=c.ref_des,
                    zone=self.zone.get(c.ref_des, "general")
                    if cp is None
                    else ("mcu" if c.category == "mcu_wireless" else "connector"),
                    side=Side.TOP,
                    rotation_deg=0.0,
                    near=near,
                    edge=cp.edge.value if cp else None,
                    notes=(
                        "rotate so the mating face points off the board"
                        if cp is not None and c.category != "mcu_wireless"
                        else ""
                    ),
                )
            )
        return out

    def zones(self) -> list[Zone]:
        boxes: dict[str, tuple[float, float, float, float]] = {}
        for p in self.placed.values():
            z = p.zone
            b = p.bbox
            if z in boxes:
                o = boxes[z]
                boxes[z] = (min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3]))
            else:
                boxes[z] = b
        out = []
        for name in ZONE_ORDER:
            if name in boxes:
                x0, y0, x1, y1 = boxes[name]
                out.append(
                    Zone(
                        name=name,
                        kind=name,
                        rect=Rect(x_mm=x0, y_mm=y0, w_mm=max(x1 - x0, 0.1), h_mm=max(y1 - y0, 0.1)),
                        description=ZONE_TEXT[name],
                    )
                )
        return out


def place(plan: DesignPlan) -> tuple[PlacementPlan, list[Keepout], list[Finding]]:
    pl = Placer(plan)
    pl.holes()
    pl.edges()
    pl.anchors()
    pl.leftovers()
    keepouts = pl.rf_keepouts()
    placements = [
        ComponentPlacement(
            ref=p.comp.ref_des,
            x_mm=p.x,
            y_mm=p.y,
            rotation_deg=p.rot,
            side=Side.TOP,
            zone=p.zone,
            bbox=Rect(
                x_mm=p.bbox[0],
                y_mm=p.bbox[1],
                w_mm=max(p.bbox[2] - p.bbox[0], 0.01),
                h_mm=max(p.bbox[3] - p.bbox[1], 0.01),
            ),
        )
        for p in sorted(pl.placed.values(), key=lambda q: q.comp.ref_des)
    ]
    zones = pl.zones()
    for k in keepouts:
        zones.append(Zone(name=k.name, kind="rf", rect=k.rect, description=ZONE_TEXT["rf"]))
    plan_out = PlacementPlan(
        zones=zones,
        hints=pl.hints(),
        placements=placements,
        notes=[
            "Coordinates are footprint origins in mm from the board's top-left corner.",
            "This is a deterministic starting placement; refine it in KiCad.",
            "Edge connectors are flush with their edge at 0 deg; rotate them so the mating "
            "face points off the board.",
        ],
    )
    return plan_out, keepouts, pl.findings


def run_placement_stage(plan: DesignPlan) -> None:
    placement, keepouts, findings = place(plan)
    old = {p.ref: p for p in plan.placement.placements} if plan.placement else {}
    locked = {c.ref_des for c in plan.components if c.locked}
    if old and locked:
        merged = []
        for p in placement.placements:
            merged.append(old[p.ref] if p.ref in locked and p.ref in old else p)
        placement.placements = merged
    plan.placement = placement
    assert plan.constraints is not None
    plan.constraints.electrical.rf_keepouts = keepouts
    plan.findings += findings
