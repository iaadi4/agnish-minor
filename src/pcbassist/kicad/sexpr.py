"""Minimal, dependency-free S-expression parser and serializer for KiCad files.

KiCad files (``.kicad_sym``, ``.kicad_mod``, ``.kicad_pcb``, ``.net``, ``.kicad_sch``) are
S-expressions. We model them with three Python types:

* ``list``      -> an S-expression list ``( ... )``
* ``Symbol``    -> a bare atom (``footprint``, ``yes``, ``smd``, ``F.Cu`` ...)
* ``str``       -> a quoted string (``"Capacitor_SMD:C_0402_1005Metric"``)
* ``int``/``float`` -> numbers

Keeping bare atoms and quoted strings distinct is what makes round-tripping exact: KiCad
cares whether ``(layer "F.Cu")`` or ``(layer F.Cu)`` is written in some contexts.
"""

from __future__ import annotations

import math
from collections.abc import Iterator


class Symbol(str):
    """A bare (unquoted) atom."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"Symbol({str.__repr__(self)})"


SExpr = list["SExpr"] | Symbol | str | int | float


class SExprError(ValueError):
    """Raised on malformed S-expression input."""


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _tokenize(text: str) -> Iterator[tuple[str, object, int]]:
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            yield ("(", None, i)
            i += 1
        elif c == ")":
            yield (")", None, i)
            i += 1
        elif c == '"':
            start = i
            i += 1
            buf: list[str] = []
            while True:
                if i >= n:
                    raise SExprError(f"unterminated string starting at offset {start}")
                ch = text[i]
                if ch == "\\":
                    if i + 1 >= n:
                        raise SExprError(f"dangling escape at offset {i}")
                    nxt = text[i + 1]
                    buf.append(_ESCAPES.get(nxt, nxt))
                    i += 2
                elif ch == '"':
                    i += 1
                    break
                else:
                    buf.append(ch)
                    i += 1
            yield ("str", "".join(buf), start)
        else:
            start = i
            while i < n and text[i] not in ' \t\r\n()"':
                i += 1
            yield ("atom", text[start:i], start)


def _atom(tok: str) -> SExpr:
    # KiCad numbers: ints, floats, exponent notation. Anything else is a symbol.
    try:
        if tok.lstrip("+-").isdigit():
            return int(tok)
        f = float(tok)
        if math.isfinite(f):
            return f
    except ValueError:
        pass
    return Symbol(tok)


def parse(text: str) -> SExpr:
    """Parse exactly one top-level S-expression."""
    stack: list[list[SExpr]] = []
    result: SExpr | None = None
    for kind, value, pos in _tokenize(text):
        if result is not None:
            if kind == ")":
                raise SExprError(f"unbalanced ')' at offset {pos}")
            raise SExprError(f"trailing content after top-level expression at offset {pos}")
        if kind == "(":
            stack.append([])
        elif kind == ")":
            if not stack:
                raise SExprError(f"unbalanced ')' at offset {pos}")
            done = stack.pop()
            if stack:
                stack[-1].append(done)
            else:
                result = done
        else:
            item: SExpr = value if kind == "str" else _atom(str(value))  # type: ignore[assignment]
            if not stack:
                result = item
            else:
                stack[-1].append(item)
    if stack:
        raise SExprError("unbalanced '(' — input ended inside a list")
    if result is None:
        raise SExprError("empty input")
    return result


def _fmt_number(v: int | float) -> str:
    if isinstance(v, bool):  # bool is an int subclass; KiCad uses yes/no symbols instead
        raise TypeError("use Symbol('yes')/Symbol('no') instead of bool")
    if isinstance(v, int):
        return str(v)
    if not math.isfinite(v):
        raise SExprError(f"non-finite number {v!r} cannot be serialized")
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _fmt_string(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{out}"'


def _fmt_atom(x: SExpr) -> str:
    if isinstance(x, Symbol):
        return str(x)
    if isinstance(x, str):
        return _fmt_string(x)
    if isinstance(x, (int, float)):
        return _fmt_number(x)
    raise TypeError(f"cannot serialize {type(x).__name__}")


def _inline(x: SExpr) -> str:
    if not isinstance(x, list):
        return _fmt_atom(x)
    return "(" + " ".join(_inline(e) for e in x) + ")"


def dumps(expr: SExpr, indent: str = "\t", width: int = 88) -> str:
    """Serialize to KiCad-style text.

    Lists without nested lists, or whose inline form fits in ``width`` columns, are written
    on one line; otherwise leading atoms stay on the head line and each remaining element
    goes on its own indented line (the layout KiCad itself writes).
    """

    def emit(x: SExpr, depth: int) -> str:
        if not isinstance(x, list):
            return _fmt_atom(x)
        flat = _inline(x)
        has_lists = any(isinstance(e, list) for e in x)
        if not has_lists or (len(flat) + len(indent) * depth <= width and depth > 0):
            return flat
        first = ["(" + (_fmt_atom(x[0]) if x and not isinstance(x[0], list) else "")]
        rest = x[1:] if x and not isinstance(x[0], list) else x
        while rest and not isinstance(rest[0], list):
            first.append(_fmt_atom(rest[0]))
            rest = rest[1:]
        lines = [" ".join(first)]
        pad = indent * (depth + 1)
        lines.extend(pad + emit(e, depth + 1) for e in rest)
        lines.append(indent * depth + ")")
        return "\n".join(lines)

    return emit(expr, 0) + "\n"


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def head(x: SExpr) -> str | None:
    """Return the first atom of a list (its 'tag'), else None."""
    if isinstance(x, list) and x and isinstance(x[0], Symbol):
        return str(x[0])
    return None


def children(x: SExpr, tag: str) -> list[list[SExpr]]:
    """Direct child lists whose head is ``tag``."""
    if not isinstance(x, list):
        return []
    return [c for c in x if isinstance(c, list) and head(c) == tag]


def child(x: SExpr, tag: str) -> list[SExpr] | None:
    found = children(x, tag)
    return found[0] if found else None


def walk(x: SExpr, tag: str) -> Iterator[list[SExpr]]:
    """Depth-first iteration over every nested list with head ``tag``."""
    if isinstance(x, list):
        if head(x) == tag:
            yield x
        for c in x:
            yield from walk(c, tag)


def value(x: list[SExpr] | None, index: int = 1) -> SExpr | None:
    if x is None or len(x) <= index:
        return None
    return x[index]


def properties(x: SExpr) -> dict[str, str]:
    """``(property "Name" "Value" ...)`` children as a dict."""
    out: dict[str, str] = {}
    for p in children(x, "property"):
        if len(p) >= 3 and isinstance(p[1], str) and isinstance(p[2], str):
            out[str(p[1])] = str(p[2])
    return out
