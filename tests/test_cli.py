from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pcbassist.cli import app

runner = CliRunner()


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


@pytest.mark.parametrize(
    "args",
    [
        ["generate", "x", "--mock-llm"],
        ["check", "p.json"],
        ["refine", "p.json", "-f", "x"],
        ["export", "p.json"],
        ["diff", "a", "b"],
        ["serve"],
    ],
)
def test_unimplemented_commands_exit_2(args: list[str]) -> None:
    r = runner.invoke(app, args)
    assert r.exit_code == 2
