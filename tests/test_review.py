from __future__ import annotations

from pathlib import Path

import pytest

from conftest import example_prompt
from pcbassist.library import ComponentLibrary
from pcbassist.llm import AuditLog, MockLLMClient
from pcbassist.review import (
    ReviewError,
    Session,
    SessionError,
    add_comment,
    apply_decision,
    apply_directives,
    classify_feedback,
    diff_plans,
    format_diff,
    refine_plan,
    resolve_comment,
    resolve_plan_path,
    review_summary,
)
from pcbassist.schemas import CheckStatus, DesignPlan, ItemRef, ItemStatus, ReviewStatus

T = ItemRef


def test_session_versions_and_immutability(plan_copy, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    s = Session(tmp_path / "s")
    assert not s.exists() and s.versions() == [] and s.next_version() == 1
    p = plan_copy("iot_env_sensor")
    s.save(p)
    assert s.exists() and s.versions() == [1] and s.load().version == 1
    add_comment(p, T(kind="component", id="U3"), "looks fine")
    s.save(p)  # review metadata may change in place
    assert s.load(1).comments
    p.components[0].mpn = "SOMETHING-ELSE"
    with pytest.raises(SessionError, match="different design content"):
        s.save(p)
    p2 = s.load().model_copy(update={"version": 2, "parent_version": 1})
    s.save(p2)
    assert s.versions() == [1, 2] and s.load().version == 2
    with pytest.raises(SessionError):
        s.load(9)
    assert resolve_plan_path(tmp_path / "s")[1] is None
    assert resolve_plan_path(tmp_path / "s" / "plan.v2.json")[1] == 2
    assert resolve_plan_path(tmp_path / "s" / "plan.json")[0].root == tmp_path / "s"


def test_comments_and_item_decisions(plan_copy, library: ComponentLibrary) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    c = add_comment(p, T(kind="net", id="GND"), "pour on both layers?")
    assert c.id == "C-1" and p.review_status == ReviewStatus.IN_REVIEW
    assert resolve_comment(p, "C-1").resolved
    with pytest.raises(ReviewError):
        resolve_comment(p, "C-9")
    with pytest.raises(ReviewError, match="no component"):
        add_comment(p, T(kind="component", id="U99"), "x")
    apply_decision(p, T(kind="component", id="U3"), "approve")
    apply_decision(p, T(kind="component", id="U2"), "reject", note="too much Iq")
    apply_decision(p, T(kind="net", id="GND"), "lock")
    apply_decision(p, T(kind="placement", id="U3"), "lock")
    apply_decision(p, T(kind="constraint", id="*"), "lock")
    assert p.component("U3").status == ItemStatus.APPROVED  # type: ignore[union-attr]
    assert p.component("U2").status == ItemStatus.REJECTED  # type: ignore[union-attr]
    assert p.netlist.net("GND").locked and p.component("U3").locked  # type: ignore[union-attr]
    assert p.constraints.locked  # type: ignore[union-attr]
    apply_decision(p, T(kind="net", id="GND"), "unlock")
    assert not p.netlist.net("GND").locked  # type: ignore[union-attr]
    s = review_summary(p)
    assert s["approved"] == 1 and s["rejected"] == 1 and s["locked"] == 1
    assert [d.id for d in p.decisions] == ["D-1", "D-2", "D-3", "D-4", "D-5", "D-6"]
    with pytest.raises(ReviewError, match="unknown decision"):
        apply_decision(p, T(kind="component", id="U3"), "bless")
    with pytest.raises(ReviewError, match="cannot be locked"):
        apply_decision(p, T(kind="routing", id="*"), "lock")
    with pytest.raises(ReviewError, match="no approval state"):
        apply_decision(p, T(kind="rail", id="VBAT"), "approve")


def test_edits(plan_copy, library: ComponentLibrary) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    apply_decision(p, T(kind="component", id="U4"), "edit", edit={"mpn": "SHTC3"}, lib=library)
    u4 = p.component("U4")
    assert u4 is not None and u4.mpn == "SHTC3" and u4.verified and u4.origin == "human"
    assert u4.status == ItemStatus.EDITED and len(u4.pins) == 5
    apply_decision(p, T(kind="component", id="U4"), "edit", edit={"mpn": "NOT-REAL-1"}, lib=library)
    assert not p.component("U4").verified  # type: ignore[union-attr]
    apply_decision(p, T(kind="placement", id="R1"), "edit", edit={"x_mm": 30.0, "y_mm": 20.0})
    pl = next(x for x in p.placement.placements if x.ref == "R1")  # type: ignore[union-attr]
    assert pl.x_mm == 30 and pl.bbox.x_mm < 30 < pl.bbox.x2  # type: ignore[union-attr]
    apply_decision(p, T(kind="rail", id="+3V3"), "edit", edit={"max_current_ma": 100})
    with pytest.raises(ReviewError, match="cannot edit"):
        apply_decision(p, T(kind="component", id="U3"), "edit", edit={"pins": []})
    with pytest.raises(ReviewError, match="needs an 'edit'"):
        apply_decision(p, T(kind="component", id="U3"), "edit")
    with pytest.raises(ReviewError, match="invalid edit"):
        apply_decision(p, T(kind="net_class", id="Default"), "edit", edit={"trace_width_mm": -1})
    with pytest.raises(ReviewError, match="cannot be edited"):
        apply_decision(p, T(kind="routing", id="*"), "edit", edit={"notes": []})


def test_plan_approval_requires_passing_checks(plan_copy) -> None:  # type: ignore[no-untyped-def]
    bad = plan_copy("flawed_coin_wifi")
    with pytest.raises(ReviewError, match="FAIL"):
        apply_decision(bad, T(kind="plan", id="*"), "approve")
    apply_decision(bad, T(kind="plan", id="*"), "approve", force=True, note="demo only")
    assert bad.review_status == ReviewStatus.APPROVED
    good = plan_copy("ble_coin_tag")
    assert good.check_summary.status == CheckStatus.PASS_WITH_WARNINGS
    apply_decision(good, T(kind="plan", id="*"), "reject")
    assert good.review_status == ReviewStatus.REJECTED
    with pytest.raises(ReviewError, match="only be approved or rejected"):
        apply_decision(good, T(kind="plan", id="*"), "lock")


def test_classify_feedback(library: ComponentLibrary) -> None:
    c = classify_feedback
    assert c("Replace the SHT40 with a BME280", library).stage == "components"
    assert c("use a cheaper regulator").stage == "components"
    assert c("move the LED closer to the edge").stage == "placement"
    assert c("route the USB pair on top").stage == "routing"
    assert c("make the board 60 x 40 mm").stage == "constraints"
    assert c("go to 4 layers").stage == "constraints"
    assert c("split this into separate rail blocks, new architecture").stage == "architecture"
    assert c("it should also log GPS position").stage == "requirements"


def test_directives(plan_copy) -> None:  # type: ignore[no-untyped-def]
    p = plan_copy("iot_env_sensor")
    notes = apply_directives(p, "please use 60x40 mm and 4 layers")
    assert p.requirements.size.max_width_mm == 60 and p.requirements.layer_count_target == 4  # type: ignore[union-attr]
    assert len(notes) == 2
    assert apply_directives(DesignPlan(prompt="x"), "60 x 40 mm") == []


def test_refine_swaps_a_part_and_keeps_review_state(plans, library: ComponentLibrary) -> None:  # type: ignore[no-untyped-def]
    base = plans["iot_env_sensor"].model_copy(deep=True)
    apply_decision(base, T(kind="component", id="U3"), "approve")
    apply_decision(base, T(kind="component", id="U2"), "lock")
    audit = AuditLog(None)
    fb = "Replace the SHT40 with a BME280 so we also get barometric pressure."
    new, cls = refine_plan(base, fb, client=MockLLMClient(), lib=library, audit=audit)
    assert cls.stage == "components"
    assert new.version == 2 and new.parent_version == 1 and new.feedback_history == [fb]
    assert new.stages_run[0] == "components"
    mpns = {c.mpn for c in new.components}
    assert "BME280" in mpns and "SHT40-AD1B-R3" not in mpns
    assert new.component("U4").mpn == "BME280"  # type: ignore[union-attr]
    assert new.component("U3").status == ItemStatus.APPROVED  # type: ignore[union-attr]
    assert new.component("U2").locked  # type: ignore[union-attr]
    assert "iot_env_sensor_refine_bme280" in (new.llm.fixture_set or "")  # type: ignore[union-attr]
    assert not any(f.rule == "MOCK-NEAREST" for f in new.findings)
    d = diff_plans(base, new)
    assert ("U4", "mpn", "SHT40-AD1B-R3", "BME280") in d.components_changed
    assert any(c.startswith("C10 ") for c in d.components_added)
    text = format_diff(d)
    assert "~ U4.mpn: SHT40-AD1B-R3 -> BME280" in text
    assert any(e.kind == "review" for e in audit.entries)


def test_refine_deterministic_stage_and_locked_net(plans, library: ComponentLibrary) -> None:  # type: ignore[no-untyped-def]
    base = plans["relay_motor_12v"].model_copy(deep=True)
    base.netlist.net("VIN").locked = True  # type: ignore[union-attr]
    client = MockLLMClient()
    new, cls = refine_plan(base, "Make the board 100 x 70 mm", client=client, lib=library)
    assert cls.stage == "constraints" and client.calls == []  # no LLM involved
    assert new.constraints.mechanical.outline.width_mm == 100  # type: ignore[union-attr]
    assert new.netlist.net("VIN").locked  # type: ignore[union-attr]
    d = diff_plans(base, new)
    assert d.outline_changed == ("80 x 60 mm", "100 x 70 mm")
    assert format_diff(diff_plans(new, new)).endswith("no changes")
    with pytest.raises(ValueError, match="unknown stage"):
        refine_plan(base, "x", client=client, lib=library, stage="nope")


def test_rejected_part_is_reported_on_refine(plans, library: ComponentLibrary) -> None:  # type: ignore[no-untyped-def]
    base = plans["iot_env_sensor"].model_copy(deep=True)
    apply_decision(base, T(kind="component", id="U4"), "reject", note="want pressure too")
    new, _ = refine_plan(
        base,
        "Pick something else for the sensor",
        client=MockLLMClient(),
        lib=library,
        stage="components",
    )
    # the mock answers with the original selection, which re-proposes the rejected part:
    assert any(f.rule == "SEL-REJECTED-AGAIN" for f in new.findings)
    assert "SHT40-AD1B-R3" not in {c.mpn for c in new.components}


def test_generate_writes_audit_without_secrets(
    tmp_path: Path, library: ComponentLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pcbassist.pipeline import generate_plan

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-SECRETSECRETSECRET")
    audit = AuditLog(tmp_path / "audit.jsonl")
    generate_plan(example_prompt("ble_coin_tag"), client=MockLLMClient(), lib=library, audit=audit)
    text = (tmp_path / "audit.jsonl").read_text()
    assert "llm_call" in text and "SECRETSECRET" not in text
