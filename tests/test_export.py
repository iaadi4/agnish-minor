from __future__ import annotations

import csv
import io
import json
import os
import stat
from pathlib import Path

import pytest

from conftest import EXAMPLES
from pcbassist.export import export_plan, find_kicad_cli, run_drc, write_bom, write_netlist
from pcbassist.export.kicad_pcb import build_pcb
from pcbassist.export.kicad_pro import build_project
from pcbassist.export.kicad_sch import SymbolLayout, build_schematic
from pcbassist.kicad.sexpr import child, children, head, parse, value, walk
from pcbassist.schemas import DISCLAIMER, DesignPlan
from test_checks import SAMPLE_DRC


@pytest.mark.parametrize("name", EXAMPLES)
def test_netlist_roundtrips_plan_connectivity(plans: dict[str, DesignPlan], name: str) -> None:
    plan = plans[name]
    tree = parse(write_netlist(plan, "2026-11-26"))
    assert head(tree) == "export" and value(child(tree, "version")) == "E"
    nets = {}
    for n in walk(child(tree, "nets"), "net"):
        nodes = {(value(child(x, "ref")), value(child(x, "pin"))) for x in children(n, "node")}
        nets[value(child(n, "name"))] = nodes
    assert plan.netlist is not None
    for n in plan.netlist.nets:
        assert nets[n.name] == {(c.ref, c.pin) for c in n.connections}
    comps = {value(child(c, "ref")) for c in children(child(tree, "components"), "comp")}
    assert comps == {c.ref_des for c in plan.components if c.verified}
    assert DISCLAIMER in write_netlist(plan)


@pytest.mark.parametrize("name", EXAMPLES)
def test_pcb_contains_placed_footprints_with_nets(plans: dict[str, DesignPlan], name: str) -> None:
    plan = plans[name]
    tree = build_pcb(plan)
    assert head(tree) == "kicad_pcb"
    fps = children(tree, "footprint")
    placed = {p.ref for p in plan.placement.placements}  # type: ignore[union-attr]
    refs = set()
    for fp in fps:
        props = {str(p[1]): p[2] for p in children(fp, "property")}
        refs.add(props["Reference"])
        assert props["MPN"]
    assert refs == placed
    declared = {str(n[2]) for n in children(tree, "net")}
    assert plan.netlist is not None
    assert {n.name for n in plan.netlist.nets} <= declared
    # every connected pad carries its net
    pad_nets = {}
    for fp in fps:
        ref = next(str(p[2]) for p in children(fp, "property") if p[1] == "Reference")
        for pad in children(fp, "pad"):
            net = child(pad, "net")
            if net is not None:
                pad_nets[(ref, str(pad[1]))] = str(net[2])
    for n in plan.netlist.nets:
        for c in n.connections:
            if c.ref in placed:
                assert pad_nets[(c.ref, c.pin)] == n.name
    edges = [x for x in tree if head(x) in ("gr_line", "gr_arc")]
    assert len(edges) == 8
    uuids = [str(u[1]) for u in walk(tree, "uuid")]
    assert len(uuids) == len(set(uuids)), "duplicate UUIDs"


def test_pcb_export_is_deterministic(plans: dict[str, DesignPlan]) -> None:
    from pcbassist.export.kicad_pcb import write_pcb

    p = plans["relay_motor_12v"]
    assert write_pcb(p) == write_pcb(p)


def test_project_netclasses_and_rules(plans: dict[str, DesignPlan]) -> None:
    proj = build_project(plans["relay_motor_12v"])
    classes = {c["name"]: c for c in proj["net_settings"]["classes"]}
    assert {"Default", "Power", "Ground"} <= set(classes)
    pats = {p["pattern"]: p["netclass"] for p in proj["net_settings"]["netclass_patterns"]}
    assert pats["VIN"] == "Power" and pats["GND"] == "Ground"
    assert proj["board"]["design_settings"]["rules"]["min_track_width"] == 0.15
    assert proj["text_variables"]["DISCLAIMER"] == DISCLAIMER


@pytest.mark.parametrize("name", EXAMPLES)
def test_schematic_labels_match_netlist(plans: dict[str, DesignPlan], name: str) -> None:
    plan = plans[name]
    tree = build_schematic(plan)
    syms = children(tree, "symbol")
    assert {
        next(str(p[2]) for p in children(s, "property") if p[1] == "Reference") for s in syms
    } == {c.ref_des for c in plan.components if c.verified and c.pins}
    labels = [str(lb[1]) for lb in children(tree, "label")]
    ncs = children(tree, "no_connect")
    assert plan.netlist is not None
    pins_on_nets = sum(
        len([c for c in n.connections if plan.component(c.ref) and plan.component(c.ref).pins])  # type: ignore[union-attr]
        for n in plan.netlist.nets
    )
    assert len(labels) == pins_on_nets
    assert len(ncs) == len(plan.netlist.no_connects)
    # all connection points on the 1.27 mm grid
    for lb in children(tree, "label"):
        at = child(lb, "at")
        for v in (at[1], at[2]):  # type: ignore[index]
            assert abs(float(v) / 1.27 - round(float(v) / 1.27)) < 1e-6  # type: ignore[arg-type]


def test_symbol_layout_balances_columns(plans: dict[str, DesignPlan]) -> None:
    mcu = next(c for c in plans["ble_coin_tag"].components if c.mpn.startswith("MDBT42Q"))
    lay = SymbolLayout(mcu)
    assert abs(len(lay.left) - len(lay.right)) <= 1
    assert len(lay.pos) == len(mcu.pins)


def test_bom_groups_and_flags(plans: dict[str, DesignPlan]) -> None:
    text = write_bom(plans["flawed_coin_wifi"])
    first, rest = text.split("\n", 1)
    assert first.startswith("# AI-GENERATED")
    rows = list(csv.DictReader(io.StringIO(rest)))
    sht = next(r for r in rows if r["MPN"] == "SHT40-AD1B-R3")
    assert sht["Qty"] == "2" and len(sht["Refs"].split()) == 2
    bad = next(r for r in rows if r["MPN"] == "MMBT3904")
    assert bad["Verified"] == "NO" and "UNVERIFIED" in bad["Notes"]
    total = sum(int(r["Qty"]) for r in rows)
    assert total == sum(1 for c in plans["flawed_coin_wifi"].components if c.in_bom)


def test_export_all_without_kicad(
    plan_copy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PCBASSIST_KICAD_CLI", str(tmp_path / "missing-cli"))
    plan = plan_copy("iot_env_sensor")
    res = export_plan(plan, tmp_path / "out")
    names = sorted(p.name for p in res.files)
    assert names == [
        "board.kicad_pcb",
        "board.kicad_pro",
        "board.kicad_sch",
        "board.net",
        "bom.csv",
        "report.md",
    ]
    assert res.drc is not None and not res.drc.ran
    report = (tmp_path / "out" / "report.md").read_text()
    for section in (
        "## Findings",
        "## Requirements",
        "## Architecture",
        "```mermaid",
        "## Components",
        "## Power tree and budget",
        "## Netlist",
        "## Constraints",
        "## Placement",
        "## Routing guidance",
        "## Review",
        DISCLAIMER,
    ):
        assert section in report
    for f in res.files:
        text = f.read_text()
        assert "AI-GENERATED" in text, f.name


def test_drc_with_fake_kicad_cli(
    plan_copy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    fake = tmp_path / "kicad-cli"
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps(SAMPLE_DRC))
    fake.write_text(f"#!/bin/sh\ncp {sample} drc.json\necho 'Found 2 violations'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PCBASSIST_KICAD_CLI", str(fake))
    assert find_kicad_cli() == str(fake)
    plan = plan_copy("iot_env_sensor")
    res = export_plan(plan, tmp_path / "out", formats=("kicad",))
    assert res.drc is not None and res.drc.ran and "Found 2 violations" in res.drc.note
    assert plan.check_summary is not None and plan.check_summary.kicad_drc_ran
    assert any(f.rule == "DRC-CLEARANCE" for f in plan.findings)


def test_drc_failure_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "kicad-cli"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PCBASSIST_KICAD_CLI", str(fake))
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    r = run_drc(pcb)
    assert not r.ran and "exit 3" in r.note
    monkeypatch.delenv("PCBASSIST_KICAD_CLI")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert not run_drc(pcb).ran


KICAD = (
    find_kicad_cli()
    if os.environ.get("PCBASSIST_KICAD_CLI") or os.environ.get("PCBASSIST_RUN_KICAD")
    else None
)


@pytest.mark.skipif(KICAD is None, reason="kicad-cli not configured (set PCBASSIST_KICAD_CLI)")
@pytest.mark.parametrize("name", ["iot_env_sensor", "relay_motor_12v"])
def test_real_kicad_drc_and_schematic(
    plan_copy, tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    import subprocess

    plan = plan_copy(name)
    out = Path.cwd() / ".pytest_kicad" / name  # mounted dir for the docker wrapper
    res = export_plan(plan, out, formats=("kicad",))
    assert res.drc is not None and res.drc.ran, res.drc.note
    report = json.loads((out / "drc.json").read_text())
    assert not [v for v in report["violations"] if v["type"] == "courtyards_overlap"]
    subprocess.run(
        [KICAD, "sch", "export", "netlist", "-o", "k.net", "board.kicad_sch"],
        cwd=out,
        check=True,
        capture_output=True,
    )
    k = parse((out / "k.net").read_text())
    nets = {
        str(value(child(n, "name"))).lstrip("/"): {
            (value(child(x, "ref")), value(child(x, "pin"))) for x in children(n, "node")
        }
        for n in walk(child(k, "nets"), "net")
    }
    assert plan.netlist is not None
    for n in plan.netlist.nets:
        assert nets[n.name] == {(c.ref, c.pin) for c in n.connections}
