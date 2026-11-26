from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import example_prompt
from pcbassist.ui.app import create_app


@pytest.fixture
def client(tmp_path: Path, library, monkeypatch: pytest.MonkeyPatch) -> TestClient:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PCBASSIST_KICAD_CLI", str(tmp_path / "none"))
    return TestClient(create_app(tmp_path / "sessions", lib=library))


def test_index_and_health(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "NOT VERIFIED FOR FABRICATION" in r.text
    h = client.get("/api/health").json()
    assert h["ok"] and h["parts"] > 60 and "qualified" in h["disclaimer"]
    assert client.get("/api/sessions").json() == []


def test_full_review_cycle(client: TestClient) -> None:
    r = client.post(
        "/api/sessions", json={"prompt": example_prompt("iot_env_sensor"), "name": "iot"}
    )
    assert r.status_code == 201
    sid = r.json()["id"]
    assert sid == "iot"
    again = client.post(
        "/api/sessions", json={"prompt": example_prompt("iot_env_sensor"), "name": "iot"}
    )
    assert again.json()["id"] == "iot-2"
    plan = client.get(f"/api/sessions/{sid}").json()
    assert plan["version"] == 1 and plan["check_summary"]["status"] == "PASS_WITH_WARNINGS"
    assert "board.kicad_pcb" in plan["_files"]
    listing = client.get("/api/sessions").json()
    assert {s["id"] for s in listing} == {"iot", "iot-2"}

    c = client.post(
        f"/api/sessions/{sid}/comments",
        json={"target": {"kind": "component", "id": "U3"}, "text": "ok?"},
    ).json()
    assert client.post(f"/api/sessions/{sid}/comments/{c['id']}/resolve").json()["resolved"]
    assert client.post(f"/api/sessions/{sid}/comments/C-99/resolve").status_code == 404
    bad = client.post(
        f"/api/sessions/{sid}/comments", json={"target": {"kind": "net", "id": "NOPE"}, "text": "x"}
    )
    assert bad.status_code == 400
    d = client.post(
        f"/api/sessions/{sid}/decisions",
        json={"target": {"kind": "component", "id": "U3"}, "decision": "approve"},
    )
    assert d.status_code == 201 and d.json()["version"] == 1
    e = client.post(
        f"/api/sessions/{sid}/decisions",
        json={
            "target": {"kind": "placement", "id": "R1"},
            "decision": "edit",
            "edit": {"x_mm": 20, "y_mm": 20},
        },
    )
    assert e.json()["version"] == 2
    assert (
        client.post(
            f"/api/sessions/{sid}/decisions",
            json={"target": {"kind": "component", "id": "U3"}, "decision": "zap"},
        ).status_code
        == 400
    )

    ref = client.post(
        f"/api/sessions/{sid}/refine",
        json={"feedback": "Replace the SHT40 with a BME280 so we also get barometric pressure."},
    )
    assert ref.status_code == 201
    body = ref.json()
    assert body["version"] == 3 and body["stage"] == "components" and "BME280" in body["diff"]
    diff = client.get(f"/api/sessions/{sid}/diff", params={"a": 1, "b": 3}).json()
    assert "U4.mpn" in diff["text"]
    f = client.get(f"/api/sessions/{sid}/files/3/bom.csv")
    assert f.status_code == 200 and "BME280" in f.text
    assert client.get(f"/api/sessions/{sid}/files/3/..secret").status_code in (400, 404)
    assert client.get(f"/api/sessions/{sid}/files/3/missing.txt").status_code == 404
    audit = client.get(f"/api/sessions/{sid}/audit").json()
    assert any(a["kind"] == "llm_call" for a in audit) and any(a["kind"] == "review" for a in audit)
    v1 = client.get(f"/api/sessions/{sid}", params={"version": 1}).json()
    assert v1["version"] == 1
    assert client.get(f"/api/sessions/{sid}", params={"version": 99}).status_code == 404
    assert client.get("/api/sessions/nope").status_code == 404
    assert client.get("/api/sessions/bad id!").status_code in (400, 404)
    assert (
        client.post(
            f"/api/sessions/{sid}/refine", json={"feedback": "xyz", "stage": "bogus"}
        ).status_code
        == 400
    )


def test_live_mode_without_key_is_a_clear_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    r = client.post("/api/sessions", json={"prompt": "a board please", "mock": False})
    assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.json()["detail"]
