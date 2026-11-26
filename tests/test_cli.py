from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pcbassist.cli import app

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]


def test_version() -> None:
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0 and r.stdout.strip() == "0.1.0"


def test_library_commands() -> None:
    r = runner.invoke(app, ["library", "list", "-c", "ldo"])
    assert r.exit_code == 0 and "AP2112K-3.3TRG1" in r.stdout
    r = runner.invoke(app, ["library", "search", "humidity", "--supply", "3.3"])
    assert r.exit_code == 0 and "SHT40" in r.stdout
    r = runner.invoke(app, ["library", "show", "MCP73831T-2ACI/OT"])
    assert r.exit_code == 0 and json.loads(r.stdout)["category"] == "charger"
    r = runner.invoke(app, ["library", "show", "NOPE"])
    assert r.exit_code == 1


def test_library_validate_passes() -> None:
    r = runner.invoke(app, ["library", "validate"])
    assert r.exit_code == 0, r.stdout
    assert "parts checked" in r.stdout


def test_schema_commands(tmp_path: Path) -> None:
    r = runner.invoke(app, ["schema", "export", "-o", str(tmp_path)])
    assert r.exit_code == 0 and (tmp_path / "DesignPlan.schema.json").exists()
    r = runner.invoke(app, ["schema", "show", "Requirements"])
    assert r.exit_code == 0 and "open_questions" in r.stdout
    assert runner.invoke(app, ["schema", "show", "Nope"]).exit_code == 1


def _run(*args: str, ok: bool = True) -> str:
    res = runner.invoke(app, list(args))
    if ok:
        assert res.exit_code == 0, res.output + str(res.exception)
    return res.output


@pytest.fixture
def no_kicad(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PCBASSIST_KICAD_CLI", str(tmp_path / "no-kicad-cli"))


def test_generate_review_refine_diff_flow(tmp_path: Path, no_kicad: None) -> None:
    out = tmp_path / "iot"
    prompt = ROOT / "examples" / "iot_env_sensor" / "prompt.txt"
    text = _run("generate", "-f", str(prompt), "--out", str(out), "--mock-llm")
    assert "PASS_WITH_WARNINGS" in text and "AI-GENERATED" in text
    assert (out / "plan.v1.json").exists() and (out / "v1" / "board.kicad_pcb").exists()
    assert (out / "audit.jsonl").read_text().count("llm_call") >= 3
    res = runner.invoke(app, ["generate", "x y z", "--out", str(out), "--mock-llm"])
    assert res.exit_code == 1 and "already holds a session" in res.output
    _run("review", "comment", str(out), "-t", "component:U3", "-m", "antenna clearance?")
    _run("review", "approve", str(out), "-t", "component:U3")
    _run("review", "lock", str(out), "-t", "net:GND")
    _run("review", "unlock", str(out), "-t", "net:GND")
    _run("review", "reject", str(out), "-t", "component:D2", "--note", "no LED needed")
    assert "C-1" in _run("review", "status", str(out))
    text = _run(
        "refine",
        str(out),
        "-f",
        "Replace the SHT40 with a BME280 so we also get barometric pressure.",
        "--mock-llm",
    )
    assert (
        "~ U4.mpn: SHT40-AD1B-R3 -> BME280" in text
        and "Regenerated from stage 'components'" in text
    )
    assert (out / "plan.v2.json").exists() and (out / "v2" / "report.md").exists()
    assert "U4.mpn" in _run("diff", str(out / "plan.v1.json"), str(out / "plan.v2.json"))
    data = json.loads(_run("diff", str(out / "plan.v1.json"), str(out / "plan.v2.json"), "--json"))
    assert data["a_version"] == 1 and data["b_version"] == 2
    text = _run(
        "review", "edit", str(out), "-t", "placement:R1", "--set", "x_mm=20", "--set", "y_mm=20"
    )
    assert "new version" in text and (out / "plan.v3.json").exists()
    res = runner.invoke(app, ["review", "edit", str(out), "-t", "component:U3", "--set", "pins=[]"])
    assert res.exit_code == 1
    res = runner.invoke(app, ["review", "approve", str(out), "-t", "bogus"])
    assert res.exit_code == 1 and "kind:id" in res.output


def test_check_and_export_commands(tmp_path: Path, no_kicad: None) -> None:
    out = tmp_path / "flawed"
    prompt = ROOT / "examples" / "flawed_coin_wifi" / "prompt.txt"
    _run("generate", "-f", str(prompt), "--out", str(out), "--mock-llm", "--no-drc")
    res = runner.invoke(app, ["check", str(out)])
    assert res.exit_code == 1 and "ERC-I2C-ADDRESS" in res.output
    res = runner.invoke(app, ["review", "approve", str(out), "-t", "plan:*"])
    assert res.exit_code == 1 and "FAIL" in res.output
    _run("review", "approve", str(out), "-t", "plan:*", "--force")
    files = _run(
        "export", str(out / "plan.v1.json"), "--format", "bom,report", "--out", str(tmp_path / "x")
    )
    assert "bom.csv" in files and "report.md" in files
    res = runner.invoke(app, ["export", str(out), "--format", "gerber"])
    assert res.exit_code == 1


def test_generate_needs_key_or_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    res = runner.invoke(app, ["generate", "a board", "--out", str(tmp_path / "s")])
    assert res.exit_code == 2 and "ANTHROPIC_API_KEY" in res.output
    res = runner.invoke(app, ["generate", "--out", str(tmp_path / "s"), "--mock-llm"])
    assert res.exit_code == 1
