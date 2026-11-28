"""KiCad 8 board file (``.kicad_pcb``) with placed library footprints, nets and outline.

Footprints are embedded verbatim from the vendored KiCad 8 library (so pads, courtyards
and the modules' own antenna keepout zones are real) with deterministic UUIDs, the
reference/value filled in and every pad assigned its net. No tracks are routed.
"""

from __future__ import annotations

import copy
import math

from ..kicad.footprint import load_footprint_sexpr
from ..kicad.sexpr import SExpr, Symbol, child, children, dumps, head
from ..schemas import DISCLAIMER, DesignPlan
from .common import BOARD_ORIGIN, SHORT_DISCLAIMER, det_uuid, tool_string

S = Symbol
PCB_VERSION = 20240108

LAYERS: list[tuple[int, str, str, str | None]] = [
    (0, "F.Cu", "signal", None),
    (31, "B.Cu", "signal", None),
    (32, "B.Adhes", "user", "B.Adhesive"),
    (33, "F.Adhes", "user", "F.Adhesive"),
    (34, "B.Paste", "user", None),
    (35, "F.Paste", "user", None),
    (36, "B.SilkS", "user", "B.Silkscreen"),
    (37, "F.SilkS", "user", "F.Silkscreen"),
    (38, "B.Mask", "user", None),
    (39, "F.Mask", "user", None),
    (40, "Dwgs.User", "user", "User.Drawings"),
    (41, "Cmts.User", "user", "User.Comments"),
    (42, "Eco1.User", "user", "User.Eco1"),
    (43, "Eco2.User", "user", "User.Eco2"),
    (44, "Edge.Cuts", "user", None),
    (45, "Margin", "user", None),
    (46, "B.CrtYd", "user", "B.Courtyard"),
    (47, "F.CrtYd", "user", "F.Courtyard"),
    (48, "B.Fab", "user", None),
    (49, "F.Fab", "user", None),
]
INNER = [(1, "In1.Cu", "signal", None), (2, "In2.Cu", "signal", None)]


def _font(size: float = 1.0) -> SExpr:
    return [S("effects"), [S("font"), [S("size"), size, size], [S("thickness"), 0.15]]]


def _stroke(w: float) -> SExpr:
    return [S("stroke"), [S("width"), w], [S("type"), S("default")]]


def _xy(x: float, y: float) -> SExpr:
    return [S("xy"), round(x, 4), round(y, 4)]


def _layers_block(n_copper: int) -> SExpr:
    rows = LAYERS[:1] + (INNER if n_copper >= 4 else []) + LAYERS[1:]
    out: list[SExpr] = [S("layers")]
    for num, name, kind, user in rows:
        item: list[SExpr] = [num, name, S(kind)]
        if user:
            item.append(user)
        out.append(item)
    return out


def _replace_uuids(node: SExpr, ref: str, counter: list[int]) -> None:
    if not isinstance(node, list):
        return
    if head(node) == "uuid" and len(node) == 2:
        counter[0] += 1
        node[1] = det_uuid("fp", ref, counter[0])
        return
    for c in node:
        _replace_uuids(c, ref, counter)


def _translate_zone(zone: list[SExpr], dx: float, dy: float) -> None:
    for poly in children(zone, "polygon") + children(zone, "filled_polygon"):
        pts = child(poly, "pts")
        if pts is None:
            continue
        for xy in pts[1:]:
            if isinstance(xy, list) and head(xy) == "xy":
                xy[1] = round(float(xy[1]) + dx, 4)  # type: ignore[arg-type]
                xy[2] = round(float(xy[2]) + dy, 4)  # type: ignore[arg-type]


def footprint_instance(
    fp_name: str,
    ref: str,
    value: str,
    x: float,
    y: float,
    rot: float,
    pad_nets: dict[str, tuple[int, str]],
    extra_props: dict[str, str],
    sym_uuid: str,
    sheetfile: str = "board.kicad_sch",
) -> SExpr | None:
    lib_fp = load_footprint_sexpr(fp_name)
    if not isinstance(lib_fp, list):
        return None
    fp = copy.deepcopy(lib_fp)
    body: list[SExpr] = [S("footprint"), fp_name]
    for item in fp[2:]:
        tag = head(item)
        if tag in ("version", "generator", "generator_version"):
            continue
        body.append(item)
        if tag == "layer":
            body.append([S("uuid"), det_uuid("footprint", ref)])
            at: list[SExpr] = [S("at"), round(x, 4), round(y, 4)]
            if rot:
                at.append(rot)
            body.append(at)
    _replace_uuids(body, ref, [0])
    # The UUID right after (layer ..) must stay the footprint's own id.
    for i, item in enumerate(body):
        if head(item) == "layer":
            body[i + 1] = [S("uuid"), det_uuid("footprint", ref)]
            break
    for item in body:
        tag = head(item)
        if not isinstance(item, list):
            continue
        if tag == "property" and len(item) >= 3:
            if item[1] == "Reference":
                item[2] = ref
            elif item[1] == "Value":
                item[2] = value
        elif tag == "pad":
            num = str(item[1])
            if rot:
                at_node = child(item, "at")
                if at_node is not None:
                    a = float(at_node[3]) if len(at_node) > 3 else 0.0  # type: ignore[arg-type]
                    if len(at_node) > 3:
                        at_node[3] = (a + rot) % 360
                    else:
                        at_node.append(rot % 360)
            if num in pad_nets:
                code, name = pad_nets[num]
                # net goes before the pad's uuid for KiCad's canonical ordering
                pos = next((i for i, x in enumerate(item) if head(x) == "uuid"), len(item))
                item.insert(pos, [S("net"), code, name])
        elif tag == "zone":
            _translate_zone(item, x, y)
    # extra properties (MPN etc.), hidden on F.Fab
    insert_at = max(i for i, x in enumerate(body) if head(x) == "property") + 1
    for k, (name, val) in enumerate(extra_props.items()):
        body.insert(
            insert_at + k,
            [
                S("property"),
                name,
                val,
                [S("at"), 0, 0, 0],
                [S("unlocked"), S("yes")],
                [S("layer"), "F.Fab"],
                [S("hide"), S("yes")],
                [S("uuid"), det_uuid("prop", ref, name)],
                _font(1.0),
            ],
        )
    # link to the schematic symbol
    attr_idx = next((i for i, x in enumerate(body) if head(x) == "attr"), None)
    path: list[SExpr] = [S("path"), f"/{sym_uuid}"]
    links: list[SExpr] = [path, [S("sheetname"), "Root"], [S("sheetfile"), sheetfile]]
    if attr_idx is None:
        attr_idx = insert_at + len(extra_props)
    for k, ln in enumerate(links):
        body.insert(attr_idx + k, ln)
    return body


def _has_own_zones(fp_name: str) -> bool:
    fp = load_footprint_sexpr(fp_name)
    return bool(children(fp, "zone")) if isinstance(fp, list) else False


def _rounded_outline(w: float, h: float, r: float, ox: float, oy: float) -> list[SExpr]:
    r = max(0.0, min(r, w / 2, h / 2))
    x0, y0, x1, y1 = ox, oy, ox + w, oy + h
    out: list[SExpr] = []
    lines = [
        ((x0 + r, y0), (x1 - r, y0)),
        ((x1, y0 + r), (x1, y1 - r)),
        ((x1 - r, y1), (x0 + r, y1)),
        ((x0, y1 - r), (x0, y0 + r)),
    ]
    for i, (a, b) in enumerate(lines):
        out.append(
            [
                S("gr_line"),
                [S("start"), round(a[0], 4), round(a[1], 4)],
                [S("end"), round(b[0], 4), round(b[1], 4)],
                _stroke(0.1),
                [S("layer"), "Edge.Cuts"],
                [S("uuid"), det_uuid("edge", i)],
            ]
        )
    if r > 0:
        k = r * (1 - math.cos(math.pi / 4))
        arcs = [
            ((x1 - r, y0), (x1 - k, y0 + k), (x1, y0 + r)),
            ((x1, y1 - r), (x1 - k, y1 - k), (x1 - r, y1)),
            ((x0 + r, y1), (x0 + k, y1 - k), (x0, y1 - r)),
            ((x0, y0 + r), (x0 + k, y0 + k), (x0 + r, y0)),
        ]
        for i, (a, m, b) in enumerate(arcs):
            out.append(
                [
                    S("gr_arc"),
                    [S("start"), round(a[0], 4), round(a[1], 4)],
                    [S("mid"), round(m[0], 4), round(m[1], 4)],
                    [S("end"), round(b[0], 4), round(b[1], 4)],
                    _stroke(0.1),
                    [S("layer"), "Edge.Cuts"],
                    [S("uuid"), det_uuid("edge-arc", i)],
                ]
            )
    return out


def _zone(
    name: str,
    net: tuple[int, str],
    layers: list[str],
    pts: list[tuple[float, float]],
    *,
    keepout: bool,
    clearance: float,
) -> SExpr:
    z: list[SExpr] = [
        S("zone"),
        [S("net"), net[0]],
        [S("net_name"), net[1]],
        [S("layers"), *layers] if len(layers) > 1 else [S("layer"), layers[0]],
        [S("uuid"), det_uuid("zone", name)],
        [S("name"), name],
        [S("hatch"), S("edge"), 0.5],
        [S("connect_pads"), [S("clearance"), 0 if keepout else clearance]],
        [S("min_thickness"), 0.25],
        [S("filled_areas_thickness"), S("no")],
    ]
    if keepout:
        z.append(
            [
                S("keepout"),
                [S("tracks"), S("not_allowed")],
                [S("vias"), S("not_allowed")],
                [S("pads"), S("not_allowed")],
                [S("copperpour"), S("not_allowed")],
                [S("footprints"), S("allowed")],
            ]
        )
    z.append([S("fill"), [S("thermal_gap"), 0.5], [S("thermal_bridge_width"), 0.5]])
    z.append([S("polygon"), [S("pts"), *[_xy(x, y) for x, y in pts]]])
    return z


def build_pcb(plan: DesignPlan, name: str = "board") -> SExpr:
    assert plan.constraints is not None and plan.netlist is not None
    mech = plan.constraints.mechanical
    ox, oy = BOARD_ORIGIN
    w, h = mech.outline.width_mm, mech.outline.height_mm
    title = plan.requirements.summary if plan.requirements else plan.prompt
    codes = {n.name: i for i, n in enumerate(plan.netlist.nets, start=1)}
    pad_nets: dict[str, dict[str, tuple[int, str]]] = {}
    for n in plan.netlist.nets:
        for conn in n.connections:
            pad_nets.setdefault(conn.ref, {})[conn.pin] = (codes[n.name], n.name)

    out: list[SExpr] = [
        S("kicad_pcb"),
        [S("version"), PCB_VERSION],
        [S("generator"), "pcbassist"],
        [S("generator_version"), tool_string().split()[-1]],
        [
            S("general"),
            [S("thickness"), mech.outline.thickness_mm],
            [S("legacy_teardrops"), S("no")],
        ],
        [S("paper"), "A4"],
        [
            S("title_block"),
            [S("title"), title],
            [S("rev"), f"v{plan.version}"],
            [S("company"), "pcbassist"],
            [S("comment"), 1, SHORT_DISCLAIMER],
            [S("comment"), 2, DISCLAIMER[:240]],
        ],
        _layers_block(mech.layer_count),
        [
            S("setup"),
            [S("pad_to_mask_clearance"), 0],
            [S("allow_soldermask_bridges_in_footprints"), S("no")],
        ],
        [S("net"), 0, ""],
    ]
    for name, code in codes.items():
        out.append([S("net"), code, name])

    placements = {p.ref: p for p in plan.placement.placements} if plan.placement else {}
    for c in plan.components:
        p = placements.get(c.ref_des)
        if p is None or not c.pins_known or not c.footprint:
            continue
        fp = footprint_instance(
            c.footprint,
            c.ref_des,
            c.value or c.mpn,
            ox + p.x_mm,
            oy + p.y_mm,
            p.rotation_deg,
            pad_nets.get(c.ref_des, {}),
            {"MPN": c.mpn, "Manufacturer": c.manufacturer or "", "Role": c.role},
            det_uuid("sym", c.ref_des),
            sheetfile=f"{name}.kicad_sch",
        )
        if fp is not None:
            out.append(fp)

    out += _rounded_outline(w, h, mech.outline.corner_radius_mm, ox, oy)
    # Disclaimer on the comments layer, just above the board
    out.append(
        [
            S("gr_text"),
            SHORT_DISCLAIMER,
            [S("at"), round(ox + w / 2, 3), round(oy - 3, 3), 0],
            [S("layer"), "Cmts.User"],
            [S("uuid"), det_uuid("disclaimer")],
            _font(1.0),
        ]
    )
    # Ground pour on the bottom layer (left unfilled; fill it in KiCad before plotting)
    gnd = codes.get("GND")
    inset = 0.5
    if gnd is not None:
        layer = "B.Cu"
        out.append(
            _zone(
                "GND_pour",
                (gnd, "GND"),
                [layer],
                [
                    (ox + inset, oy + inset),
                    (ox + w - inset, oy + inset),
                    (ox + w - inset, oy + h - inset),
                    (ox + inset, oy + h - inset),
                ],
                keepout=False,
                clearance=0.3,
            )
        )
    for ko in plan.constraints.electrical.rf_keepouts:
        owner = plan.component(ko.name.split("_")[0])
        if owner is not None and owner.footprint and _has_own_zones(owner.footprint):
            continue  # the KiCad footprint already carries the module's antenna keepout
        r = ko.rect
        out.append(
            _zone(
                ko.name,
                (0, ""),
                ko.layers,
                [
                    (ox + r.x_mm, oy + r.y_mm),
                    (ox + r.x2, oy + r.y_mm),
                    (ox + r.x2, oy + r.y2),
                    (ox + r.x_mm, oy + r.y2),
                ],
                keepout=True,
                clearance=0,
            )
        )
    return out


def write_pcb(plan: DesignPlan, name: str = "board") -> str:
    return dumps(build_pcb(plan, name))
