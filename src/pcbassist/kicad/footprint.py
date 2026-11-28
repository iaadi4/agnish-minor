"""Geometry of KiCad footprints (vendored, local KiCad install, or the bundled index).

Only what placement, DFM and export need: the courtyard bounding box relative to the
footprint origin, the pads (number, position, size, type) and the parsed S-expression.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from ..paths import data_dir
from .sexpr import SExpr, child, children, head, parse, value, walk


@dataclass(frozen=True)
class PadGeom:
    number: str
    kind: str  # smd | thru_hole | np_thru_hole | connect
    shape: str
    x: float
    y: float
    w: float
    h: float
    rot: float = 0.0
    drill: float | None = None


@dataclass(frozen=True)
class FootprintGeom:
    name: str  # "Lib:Footprint"
    path: Path
    #: courtyard bbox relative to the origin: (x_min, y_min, x_max, y_max)
    courtyard: tuple[float, float, float, float]
    pads: tuple[PadGeom, ...]
    has_courtyard: bool
    #: courtyard as a union of axis-aligned rectangles (local coords); bbox if not rectilinear
    rects: tuple[tuple[float, float, float, float], ...] = ()

    @property
    def width(self) -> float:
        return self.courtyard[2] - self.courtyard[0]

    @property
    def height(self) -> float:
        return self.courtyard[3] - self.courtyard[1]

    @property
    def smd(self) -> bool:
        """True if assembly is SMT-only (thermal vias inside an SMD pad do not count)."""
        smd_numbers = {p.number for p in self.pads if p.kind == "smd"}
        return all(
            p.kind in ("smd", "connect", "np_thru_hole") or p.number in smd_numbers
            for p in self.pads
        )

    def pad(self, number: str) -> PadGeom | None:
        return next((p for p in self.pads if p.number == number), None)

    def pad_extent(self) -> tuple[float, float, float, float]:
        if not self.pads:
            return self.courtyard
        xs = [p.x - p.w / 2 for p in self.pads] + [p.x + p.w / 2 for p in self.pads]
        ys = [p.y - p.h / 2 for p in self.pads] + [p.y + p.h / 2 for p in self.pads]
        return (min(xs), min(ys), max(xs), max(ys))


def footprint_path(name: str, root: Path | None = None) -> Path:
    lib, _, fp = name.partition(":")
    return (root or data_dir() / "kicad" / "footprints") / f"{lib}.pretty" / f"{fp}.kicad_mod"


def _num(x: SExpr | None) -> float:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0


def _xy(node: list[SExpr] | None) -> tuple[float, float] | None:
    if node is None:
        return None
    return (_num(value(node, 1)), _num(value(node, 2)))


def _layer_of(node: list[SExpr]) -> str | None:
    lay = child(node, "layer")
    v = value(lay, 1)
    return str(v) if v is not None else None


Seg = tuple[float, float, float, float]


def _courtyard_segments(fp: SExpr) -> list[Seg] | None:
    """Straight courtyard edges, or None if the courtyard has curves."""
    segs: list[Seg] = []
    if not isinstance(fp, list):
        return None
    for item in fp:
        if not isinstance(item, list) or _layer_of(item) not in ("F.CrtYd", "B.CrtYd"):
            continue
        tag = head(item)
        if tag == "fp_line":
            a, b = _xy(child(item, "start")), _xy(child(item, "end"))
            if a and b:
                segs.append((a[0], a[1], b[0], b[1]))
        elif tag == "fp_rect":
            a, b = _xy(child(item, "start")), _xy(child(item, "end"))
            if a and b:
                segs += [
                    (a[0], a[1], b[0], a[1]),
                    (b[0], a[1], b[0], b[1]),
                    (b[0], b[1], a[0], b[1]),
                    (a[0], b[1], a[0], a[1]),
                ]
        elif tag == "fp_poly":
            pts = [p for xy in walk(child(item, "pts") or [], "xy") if (p := _xy(xy))]
            for i in range(len(pts)):
                a, b = pts[i], pts[(i + 1) % len(pts)]
                segs.append((a[0], a[1], b[0], b[1]))
        elif tag in ("fp_arc", "fp_circle"):
            return None
    return segs


def rectilinear_rects(segs: list[Seg]) -> list[tuple[float, float, float, float]] | None:
    """Decompose a rectilinear outline into rectangles (even-odd rule); None if not possible."""

    def r(v: float) -> float:
        return round(v, 4)

    clean = {(r(a), r(b), r(c), r(d)) for a, b, c, d in segs if (r(a), r(b)) != (r(c), r(d))}
    vert: list[tuple[float, float, float]] = []
    ys: set[float] = set()
    for x0, y0, x1, y1 in clean:
        if x0 != x1 and y0 != y1:
            return None
        ys |= {y0, y1}
        if x0 == x1:
            vert.append((x0, min(y0, y1), max(y0, y1)))
    # merge collinear overlapping verticals so even-odd counting is robust
    merged: dict[float, list[tuple[float, float]]] = {}
    for x, lo, hi in sorted(vert):
        spans = merged.setdefault(x, [])
        if spans and lo <= spans[-1][1] + 1e-6:
            spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
        else:
            spans.append((lo, hi))
    verts = [(x, lo, hi) for x, spans in merged.items() for lo, hi in spans]
    yl = sorted(ys)
    bands: list[tuple[float, float, float, float]] = []
    for ya, yb in itertools.pairwise(yl):
        ym = (ya + yb) / 2
        xs = sorted(x for x, lo, hi in verts if lo < ym < hi)
        if len(xs) % 2:
            return None
        for i in range(0, len(xs), 2):
            bands.append((xs[i], ya, xs[i + 1], yb))
    if not bands:
        return None
    out: list[tuple[float, float, float, float]] = []
    for b in bands:
        for i, o in enumerate(out):
            if o[0] == b[0] and o[2] == b[2] and abs(o[3] - b[1]) < 1e-6:
                out[i] = (o[0], o[1], o[2], b[3])
                break
        else:
            out.append(b)
    return out


def _courtyard_points(fp: SExpr) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    if not isinstance(fp, list):
        return pts
    for item in fp:
        if not isinstance(item, list):
            continue
        tag = head(item)
        if tag not in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
            continue
        if _layer_of(item) not in ("F.CrtYd", "B.CrtYd"):
            continue
        if tag in ("fp_line", "fp_rect"):
            for t in ("start", "end"):
                p = _xy(child(item, t))
                if p:
                    pts.append(p)
        elif tag == "fp_arc":
            for t in ("start", "mid", "end"):
                p = _xy(child(item, t))
                if p:
                    pts.append(p)
        elif tag == "fp_circle":
            c = _xy(child(item, "center"))
            e = _xy(child(item, "end"))
            if c and e:
                r = math.dist(c, e)
                pts += [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
        else:
            for xy in walk(child(item, "pts") or [], "xy"):
                p = _xy(xy)
                if p:
                    pts.append(p)
    return pts


def parse_footprint(text: str, name: str, path: Path) -> FootprintGeom:
    fp = parse(text)
    pads: list[PadGeom] = []
    for pad in children(fp, "pad"):
        num = str(value(pad, 1) or "")
        kind = str(value(pad, 2) or "")
        shape = str(value(pad, 3) or "")
        at = child(pad, "at")
        size = child(pad, "size")
        drill = child(pad, "drill")
        dval = value(drill, 1)
        if isinstance(dval, (int, float)) and not isinstance(dval, bool):
            dia: float | None = float(dval)
        elif drill is not None and len(drill) > 2:  # (drill oval w h)
            dia = _num(value(drill, 2))
        else:
            dia = None
        pads.append(
            PadGeom(
                number=num,
                kind=kind,
                shape=shape,
                x=_num(value(at, 1)),
                y=_num(value(at, 2)),
                rot=_num(value(at, 3)),
                w=_num(value(size, 1)),
                h=_num(value(size, 2)),
                drill=dia,
            )
        )
    pts = _courtyard_points(fp)
    if pts:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        cy = (min(xs), min(ys), max(xs), max(ys))
        has = True
    elif pads:
        xs = [p.x - p.w / 2 for p in pads] + [p.x + p.w / 2 for p in pads]
        ys = [p.y - p.h / 2 for p in pads] + [p.y + p.h / 2 for p in pads]
        cy = (min(xs) - 0.25, min(ys) - 0.25, max(xs) + 0.25, max(ys) + 0.25)
        has = False
    else:
        cy = (-0.5, -0.5, 0.5, 0.5)
        has = False
    segs = _courtyard_segments(fp) if has else None
    rects = rectilinear_rects(segs) if segs else None
    if rects:
        xs = [r[0] for r in rects] + [r[2] for r in rects]
        ys = [r[1] for r in rects] + [r[3] for r in rects]
        if abs(min(xs) - cy[0]) > 0.01 or abs(max(ys) - cy[3]) > 0.01:
            rects = None  # decomposition disagrees with the bbox: be conservative
    return FootprintGeom(
        name=name,
        path=path,
        courtyard=cy,
        pads=tuple(pads),
        has_courtyard=has,
        rects=tuple(rects) if rects else (cy,),
    )


@cache
def load_footprint(name: str) -> FootprintGeom | None:
    """Geometry of a footprint: vendored file, local KiCad file, else the bundled index."""
    path = footprint_path(name)
    if path.exists():
        return parse_footprint(path.read_text(), name, path)
    from .fpindex import default_footprint_index, local_footprint_file

    local = local_footprint_file(name)
    if local is not None:
        return parse_footprint(local.read_text(), name, local)
    return default_footprint_index().geom(name)


def footprint_source(name: str) -> str:
    """Where :func:`load_footprint_sexpr` gets ``name`` from: vendored | local | index | none."""
    if footprint_path(name).exists():
        return "vendored"
    from .fpindex import default_footprint_index, local_footprint_file

    if local_footprint_file(name) is not None:
        return "local"
    return "index" if name in default_footprint_index() else "none"


@cache
def load_footprint_sexpr(name: str) -> SExpr | None:
    path = footprint_path(name)
    if path.exists():
        return parse(path.read_text())
    from .fpindex import local_footprint_file

    local = local_footprint_file(name)
    if local is not None:
        return parse(local.read_text())
    geom = load_footprint(name)
    return synthesize_footprint(geom) if geom is not None else None


_PAD_LAYERS = {
    "smd": ["F.Cu", "F.Paste", "F.Mask"],
    "connect": ["F.Cu", "F.Mask"],
    "thru_hole": ["*.Cu", "*.Mask"],
    "np_thru_hole": ["*.Cu", "*.Mask"],
}


def synthesize_footprint(geom: FootprintGeom) -> SExpr:
    """A minimal KiCad 8 footprint (pads + courtyard + reference) rebuilt from geometry."""
    from .sexpr import Symbol as S

    _, y0, _, y1 = geom.courtyard
    fp: list[SExpr] = [
        S("footprint"),
        geom.name.split(":", 1)[1],
        [S("layer"), "F.Cu"],
        [S("descr"), "Rebuilt by pcbassist from the KiCad footprint index (pads and courtyard)"],
        [S("attr"), S("smd") if geom.smd else S("through_hole")],
        [
            S("property"),
            "Reference",
            "REF**",
            [S("at"), 0, round(y0 - 1.0, 3), 0],
            [S("layer"), "F.SilkS"],
            [S("effects"), [S("font"), [S("size"), 1, 1], [S("thickness"), 0.15]]],
        ],
        [
            S("property"),
            "Value",
            geom.name.split(":", 1)[1],
            [S("at"), 0, round(y1 + 1.0, 3), 0],
            [S("layer"), "F.Fab"],
            [S("effects"), [S("font"), [S("size"), 1, 1], [S("thickness"), 0.15]]],
        ],
    ]
    for rx0, ry0, rx1, ry1 in geom.rects:
        fp.append(
            [
                S("fp_rect"),
                [S("start"), rx0, ry0],
                [S("end"), rx1, ry1],
                [S("stroke"), [S("width"), 0.05], [S("type"), S("solid")]],
                [S("fill"), S("none")],
                [S("layer"), "F.CrtYd"],
            ]
        )
    for p in geom.pads:
        shape = p.shape if p.shape in ("rect", "circle", "oval", "roundrect") else "rect"
        pad: list[SExpr] = [
            S("pad"),
            p.number,
            S(p.kind),
            S(shape),
            [S("at"), p.x, p.y, p.rot] if p.rot else [S("at"), p.x, p.y],
            [S("size"), p.w, p.h],
        ]
        if p.drill is not None and p.kind in ("thru_hole", "np_thru_hole"):
            pad.append([S("drill"), p.drill])
        pad.append([S("layers"), *_PAD_LAYERS.get(p.kind, ["F.Cu", "F.Mask"])])
        if shape == "roundrect":
            pad.append([S("roundrect_rratio"), 0.25])
        fp.append(pad)
    return fp


def rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """KiCad's RotatePoint: +deg is counter-clockwise on screen (y axis points down)."""
    if deg % 360 == 0:
        return (x, y)
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (round(x * c + y * s, 6), round(-x * s + y * c, 6))


def placed_bbox(
    geom: FootprintGeom, x: float, y: float, rot: float = 0.0
) -> tuple[float, float, float, float]:
    """Courtyard bbox in board coordinates for a footprint placed at (x, y, rot)."""
    x0, y0, x1, y1 = geom.courtyard
    corners = [rotate(px, py, rot) for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs = [x + c[0] for c in corners]
    ys = [y + c[1] for c in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def placed_rects(
    geom: FootprintGeom, x: float, y: float, rot: float = 0.0
) -> list[tuple[float, float, float, float]]:
    """Courtyard rectangles in board coordinates."""
    out = []
    for x0, y0, x1, y1 in geom.rects:
        corners = [rotate(px, py, rot) for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
        xs = [x + c[0] for c in corners]
        ys = [y + c[1] for c in corners]
        out.append((min(xs), min(ys), max(xs), max(ys)))
    return out


def courtyard_area(geom: FootprintGeom) -> float:
    return sum((r[2] - r[0]) * (r[3] - r[1]) for r in geom.rects)


def pad_position(
    geom: FootprintGeom, number: str, x: float, y: float, rot: float = 0.0
) -> tuple[float, float] | None:
    p = geom.pad(number)
    if p is None:
        return None
    dx, dy = rotate(p.x, p.y, rot)
    return (x + dx, y + dy)
