"""KiCad 8 schematic (``.kicad_sch``): one flat sheet, generated box symbols, net labels.

Symbols are generated from the plan's verified pin tables (so they always match the
netlist) instead of embedding the KiCad library symbols: that keeps the file valid for
KiCad 8 regardless of which symbol-library version the pins came from. Each pin gets a
short wire stub ending in a local net label; explicit no-connects get an NC flag. The
layout is a plain grid - readable for review, not a hand-drawn schematic.
"""

from __future__ import annotations

import math
import re

from ..kicad.sexpr import SExpr, Symbol, dumps
from ..schemas import DISCLAIMER, Component, DesignPlan, Pin, PinType
from .common import SHORT_DISCLAIMER, det_uuid, tool_string

S = Symbol
SCH_VERSION = 20231120
G = 1.27  # KiCad schematic grid (50 mil)
PIN_LEN = 2.54
STUB = 2.54
PAPER = ("A1", 841.0, 594.0)

_ETYPE = {
    PinType.POWER_IN: "power_in",
    PinType.POWER_OUT: "power_out",
    PinType.INPUT: "input",
    PinType.OUTPUT: "output",
    PinType.BIDIR: "bidirectional",
    PinType.PASSIVE: "passive",
    PinType.OPEN_DRAIN: "open_collector",
    PinType.NC: "no_connect",
}
_LEFT_TYPES = {PinType.POWER_IN, PinType.INPUT}


def _snap(v: float) -> float:
    return round(round(v / G) * G, 4)


def _font(hide: bool = False, justify: str | None = None) -> SExpr:
    e: list[SExpr] = [S("effects"), [S("font"), [S("size"), 1.27, 1.27]]]
    if justify:
        e.append([S("justify"), *[S(j) for j in justify.split()]])
    if hide:
        e.append([S("hide"), S("yes")])
    return e


def sym_name(c: Component) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]", "_", c.mpn)


def _natural(s: str) -> list[object]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


class SymbolLayout:
    """Pin positions of the generated box symbol (library coordinates, y up)."""

    def __init__(self, c: Component) -> None:
        pins = sorted(c.pins, key=lambda p: _natural(p.number))
        if len(pins) <= 2:
            left, right = pins[:1], pins[1:]
        else:
            left = [p for p in pins if p.type in _LEFT_TYPES or "gnd" in p.functions]
            right = [p for p in pins if p not in left]
            while len(left) - len(right) > 1:  # balance the two columns
                right.insert(0, left.pop())
            while len(right) - len(left) > 1:
                left.append(right.pop(0))
        self.left, self.right = left, right
        rows = max(len(left), len(right), 1)
        lname = max((len(p.name or p.number) for p in left), default=1)
        rname = max((len(p.name or p.number) for p in right), default=1)
        width = (lname + rname) * 1.0 + 5.08 if len(pins) > 2 else 2.54
        self.half_w = math.ceil(width / 2 / 2.54) * 2.54
        self.top = math.ceil(rows / 2) * 2.54 + G
        self.bottom = self.top - (rows + 1) * 2.54
        self.pos: dict[str, tuple[float, float, int]] = {}
        for i, p in enumerate(left):
            self.pos[p.number] = (-self.half_w - PIN_LEN, self.top - (i + 1) * 2.54, 0)
        for i, p in enumerate(right):
            self.pos[p.number] = (self.half_w + PIN_LEN, self.top - (i + 1) * 2.54, 180)

    @property
    def width(self) -> float:
        return 2 * (self.half_w + PIN_LEN)

    @property
    def height(self) -> float:
        return self.top - self.bottom


def lib_symbol(c: Component, lay: SymbolLayout) -> SExpr:
    name = sym_name(c)
    pins: list[SExpr] = []
    by_num = {p.number: p for p in c.pins}
    for num, (x, y, ang) in lay.pos.items():
        p: Pin = by_num[num]
        pins.append(
            [
                S("pin"),
                S(_ETYPE[p.type]),
                S("line"),
                [S("at"), round(x, 4), round(y, 4), ang],
                [S("length"), PIN_LEN],
                [S("name"), p.name or "~", _font()],
                [S("number"), p.number, _font()],
            ]
        )
    prefix = re.match(r"^[A-Z]+", c.ref_des)
    return [
        S("symbol"),
        f"pcbassist:{name}",
        [S("pin_names"), [S("offset"), 1.016]],
        [S("exclude_from_sim"), S("no")],
        [S("in_bom"), S("yes")],
        [S("on_board"), S("yes")],
        [
            S("property"),
            "Reference",
            prefix.group(0) if prefix else "U",
            [S("at"), 0, lay.top + G, 0],
            _font(),
        ],
        [S("property"), "Value", c.mpn, [S("at"), 0, lay.bottom - G, 0], _font()],
        [S("property"), "Footprint", c.footprint or "", [S("at"), 0, 0, 0], _font(hide=True)],
        [S("property"), "Datasheet", c.datasheet_url or "~", [S("at"), 0, 0, 0], _font(hide=True)],
        [S("property"), "Description", c.description, [S("at"), 0, 0, 0], _font(hide=True)],
        [
            S("symbol"),
            f"{name}_0_1",
            [
                S("rectangle"),
                [S("start"), -lay.half_w, lay.top],
                [S("end"), lay.half_w, lay.bottom],
                [S("stroke"), [S("width"), 0.254], [S("type"), S("default")]],
                [S("fill"), [S("type"), S("background")]],
            ],
        ],
        [S("symbol"), f"{name}_1_1", *pins],
    ]


def build_schematic(plan: DesignPlan, name: str = "board") -> SExpr:
    assert plan.netlist is not None
    root = det_uuid("schematic-root")
    pin_net = {(c.ref, c.pin): n.name for n in plan.netlist.nets for c in n.connections}
    ncs = {(x.ref, x.pin) for x in plan.netlist.no_connects}
    comps = [c for c in plan.components if c.verified and c.pins]
    lib: dict[str, SExpr] = {}
    items: list[SExpr] = []
    _, paper_w, _ = PAPER
    x_cursor, y_cursor, row_h = 30.0, 40.0, 0.0
    label_room = 22.0
    for c in comps:
        lay = SymbolLayout(c)
        lib.setdefault(sym_name(c), lib_symbol(c, lay))
        cell_w = lay.width + 2 * (STUB + label_room)
        if x_cursor + cell_w > paper_w - 20:
            x_cursor = 30.0
            y_cursor += row_h + 12.7
            row_h = 0.0
        X = _snap(x_cursor + cell_w / 2)
        Y = _snap(y_cursor + lay.top)
        row_h = max(row_h, lay.height + 7.62)
        x_cursor += cell_w
        uid = det_uuid("sym", c.ref_des)
        inst: list[SExpr] = [
            S("symbol"),
            [S("lib_id"), f"pcbassist:{sym_name(c)}"],
            [S("at"), X, Y, 0],
            [S("unit"), 1],
            [S("exclude_from_sim"), S("no")],
            [S("in_bom"), S("yes" if c.in_bom else "no")],
            [S("on_board"), S("yes")],
            [S("dnp"), S("no")],
            [S("uuid"), uid],
            [
                S("property"),
                "Reference",
                c.ref_des,
                [S("at"), X, _snap(Y - lay.top - G), 0],
                _font(),
            ],
            [
                S("property"),
                "Value",
                c.value or c.mpn,
                [S("at"), X, _snap(Y - lay.bottom + G), 0],
                _font(),
            ],
            [S("property"), "Footprint", c.footprint or "", [S("at"), X, Y, 0], _font(hide=True)],
            [
                S("property"),
                "Datasheet",
                c.datasheet_url or "~",
                [S("at"), X, Y, 0],
                _font(hide=True),
            ],
            [S("property"), "Description", c.description, [S("at"), X, Y, 0], _font(hide=True)],
            [S("property"), "MPN", c.mpn, [S("at"), X, Y, 0], _font(hide=True)],
            [S("property"), "Role", c.role, [S("at"), X, Y, 0], _font(hide=True)],
        ]
        for num in lay.pos:
            inst.append([S("pin"), num, [S("uuid"), det_uuid("pin", c.ref_des, num)]])
        inst.append(
            [
                S("instances"),
                [
                    S("project"),
                    name,
                    [S("path"), f"/{root}", [S("reference"), c.ref_des], [S("unit"), 1]],
                ],
            ]
        )
        items.append(inst)
        for num, (px, py, ang) in lay.pos.items():
            ex, ey = _snap(X + px), _snap(Y - py)
            key = (c.ref_des, num)
            if key in ncs or key not in pin_net:
                items.append(
                    [S("no_connect"), [S("at"), ex, ey], [S("uuid"), det_uuid("nc", *key)]]
                )
                continue
            dx = -STUB if ang == 0 else STUB
            lx = _snap(ex + dx)
            items.append(
                [
                    S("wire"),
                    [S("pts"), [S("xy"), ex, ey], [S("xy"), lx, ey]],
                    [S("stroke"), [S("width"), 0], [S("type"), S("default")]],
                    [S("uuid"), det_uuid("wire", *key)],
                ]
            )
            items.append(
                [
                    S("label"),
                    pin_net[key],
                    [S("at"), lx, ey, 180 if ang == 0 else 0],
                    [S("fields_autoplaced"), S("yes")],
                    _font(justify="right bottom" if ang == 0 else "left bottom"),
                    [S("uuid"), det_uuid("label", *key)],
                ]
            )
    title = plan.requirements.summary if plan.requirements else plan.prompt
    head_items: list[SExpr] = [
        S("kicad_sch"),
        [S("version"), SCH_VERSION],
        [S("generator"), "pcbassist"],
        [S("generator_version"), tool_string().split()[-1]],
        [S("uuid"), root],
        [S("paper"), PAPER[0]],
        [
            S("title_block"),
            [S("title"), title],
            [S("rev"), f"v{plan.version}"],
            [S("company"), "pcbassist"],
            [S("comment"), 1, SHORT_DISCLAIMER],
            [S("comment"), 2, DISCLAIMER[:240]],
        ],
        [S("lib_symbols"), *lib.values()],
        [
            S("text"),
            SHORT_DISCLAIMER,
            [S("exclude_from_sim"), S("no")],
            [S("at"), 30.48, 20.32, 0],
            [
                S("effects"),
                [S("font"), [S("size"), 2.54, 2.54], [S("bold"), S("yes")]],
                [S("justify"), S("left"), S("bottom")],
            ],
            [S("uuid"), det_uuid("sch-disclaimer")],
        ],
    ]
    return [
        *head_items,
        *items,
        [S("sheet_instances"), [S("path"), "/", [S("page"), "1"]]],
    ]


def write_schematic(plan: DesignPlan, name: str = "board") -> str:
    return dumps(build_schematic(plan, name))
