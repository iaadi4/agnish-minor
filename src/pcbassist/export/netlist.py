"""KiCad netlist (``.net``, S-expression export format version "E")."""

from __future__ import annotations

from ..kicad.sexpr import SExpr, Symbol, dumps
from ..schemas import DISCLAIMER, DesignPlan, PinType
from .common import det_uuid, tool_string

_PINTYPE = {
    PinType.POWER_IN: "power_in",
    PinType.POWER_OUT: "power_out",
    PinType.INPUT: "input",
    PinType.OUTPUT: "output",
    PinType.BIDIR: "bidirectional",
    PinType.PASSIVE: "passive",
    PinType.OPEN_DRAIN: "open_collector",
    PinType.NC: "no_connect",
}


def build_netlist(plan: DesignPlan, date: str = "") -> SExpr:
    S = Symbol
    comps: list[SExpr] = [S("components")]
    for c in plan.components:
        if not c.pins_known:
            continue
        lib, _, part = (c.kicad_symbol or "pcbassist:" + c.mpn).partition(":")
        fields: list[SExpr] = [
            S("fields"),
            [S("field"), [S("name"), "MPN"], c.mpn],
            [S("field"), [S("name"), "Manufacturer"], c.manufacturer or ""],
            [S("field"), [S("name"), "Role"], c.role],
        ]
        comps.append(
            [
                S("comp"),
                [S("ref"), c.ref_des],
                [S("value"), c.value or c.mpn],
                [S("footprint"), c.footprint or ""],
                [S("datasheet"), c.datasheet_url or "~"],
                [S("description"), c.description],
                fields,
                [
                    S("libsource"),
                    [S("lib"), lib],
                    [S("part"), part],
                    [S("description"), c.description],
                ],
                [S("sheetpath"), [S("names"), "/"], [S("tstamps"), "/"]],
                [S("tstamps"), det_uuid("sym", c.ref_des)],
            ]
        )
    nets: list[SExpr] = [S("nets")]
    assert plan.netlist is not None
    for code, n in enumerate(plan.netlist.nets, start=1):
        node_list: list[SExpr] = [S("net"), [S("code"), str(code)], [S("name"), n.name]]
        for conn in n.connections:
            comp = plan.component(conn.ref)
            pin = comp.pin(conn.pin) if comp else None
            node: list[SExpr] = [S("node"), [S("ref"), conn.ref], [S("pin"), conn.pin]]
            if pin is not None:
                if pin.name:
                    node.append([S("pinfunction"), pin.name])
                node.append([S("pintype"), _PINTYPE[pin.type]])
            node_list.append(node)
        nets.append(node_list)
    title = plan.requirements.summary if plan.requirements else plan.prompt
    design: SExpr = [
        S("design"),
        [S("source"), "pcbassist plan"],
        [S("date"), date],
        [S("tool"), tool_string()],
        [
            S("sheet"),
            [S("number"), "1"],
            [S("name"), "/"],
            [S("tstamps"), "/"],
            [
                S("title_block"),
                [S("title"), title],
                [S("company"), ""],
                [S("rev"), f"v{plan.version}"],
                [S("date"), date],
                [S("source"), "pcbassist"],
                [S("comment"), [S("number"), "1"], [S("value"), DISCLAIMER]],
            ],
        ],
    ]
    return [S("export"), [S("version"), "E"], design, comps, nets]


def write_netlist(plan: DesignPlan, date: str = "") -> str:
    return dumps(build_netlist(plan, date))
