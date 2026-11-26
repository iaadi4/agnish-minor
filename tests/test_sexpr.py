from __future__ import annotations

import math
import re

import pytest

from pcbassist.kicad.sexpr import (
    SExprError,
    Symbol,
    child,
    children,
    dumps,
    head,
    parse,
    properties,
    value,
    walk,
)
from pcbassist.paths import data_dir


def test_parse_atoms_strings_numbers() -> None:
    x = parse('(pad "1" smd roundrect (at -0.48 0 90) (size 0.56 0.62) (layers "F.Cu" F.Mask))')
    assert isinstance(x, list)
    assert x[0] == "pad" and isinstance(x[0], Symbol)
    assert x[1] == "1" and not isinstance(x[1], Symbol)
    at = child(x, "at")
    assert at == [Symbol("at"), -0.48, 0, 90]
    assert isinstance(at[2], int)
    assert children(x, "layers")[0][2] == Symbol("F.Mask")


def test_string_escapes_round_trip() -> None:
    src = r'(descr "a \"quoted\" back\\slash\nnewline")'
    x = parse(src)
    assert x[1] == 'a "quoted" back\\slash\nnewline'  # type: ignore[index]
    assert parse(dumps(x)) == x


def test_exponent_and_signed_numbers() -> None:
    x = parse("(v 1e-3 -2 +3 1.5E2 inf_name)")
    assert x == [Symbol("v"), 0.001, -2, 3, 150.0, Symbol("inf_name")]


@pytest.mark.parametrize(
    "bad,msg",
    [
        ("(a (b)", "unbalanced '('"),
        ("(a))", "unbalanced ')'"),
        ('(a "oops)', "unterminated string"),
        ("(a) (b)", "trailing content"),
        ("   ", "empty input"),
    ],
)
def test_parse_errors(bad: str, msg: str) -> None:
    with pytest.raises(SExprError, match=re.escape(msg)):
        parse(bad)


def test_dumps_rejects_bool_and_nonfinite() -> None:
    with pytest.raises(TypeError):
        dumps([Symbol("x"), True])
    with pytest.raises(SExprError):
        dumps([Symbol("x"), math.inf])
    with pytest.raises(TypeError):
        dumps([Symbol("x"), object()])  # type: ignore[list-item]


def test_number_formatting() -> None:
    assert dumps([Symbol("at"), 1.0, -0.0000001, 2.5, 3]) == "(at 1 0 2.5 3)\n"


def test_dumps_layout_nests_long_lists() -> None:
    x = [
        Symbol("footprint"),
        "R_0402",
        [Symbol("layer"), "F.Cu"],
        [
            Symbol("pad"),
            "1",
            Symbol("smd"),
            Symbol("rect"),
            [Symbol("at"), 0, 0],
            [Symbol("size"), 1, 1],
            [Symbol("layers"), "F.Cu", "F.Paste", "F.Mask"],
            [Symbol("uuid"), "00000000-0000-0000-0000-000000000000"],
        ],
    ]
    text = dumps(x)  # type: ignore[arg-type]
    assert text.startswith('(footprint "R_0402"\n')
    assert '\t(layer "F.Cu")\n' in text
    assert parse(text) == x


def test_real_kicad_footprint_round_trips() -> None:
    path = data_dir() / "kicad/footprints/Capacitor_SMD.pretty/C_0402_1005Metric.kicad_mod"
    tree = parse(path.read_text())
    assert head(tree) == "footprint"
    pads = children(tree, "pad")
    assert [p[1] for p in pads] == ["1", "2"]
    assert parse(dumps(tree)) == tree


def test_query_helpers() -> None:
    x = parse(
        '(symbol "R" (property "Reference" "R") (property "Value" "R") '
        '(symbol "R_0_1" (pin passive line (number "1"))) '
        '(symbol "R_1_1" (pin passive line (number "2"))))'
    )
    assert properties(x) == {"Reference": "R", "Value": "R"}
    assert [value(child(p, "number")) for p in walk(x, "pin")] == ["1", "2"]
    assert head("atom") is None
    assert children("atom", "x") == []
    assert child(x, "missing") is None
    assert value(None) is None
