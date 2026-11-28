from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import Field

from pcbassist.llm import (
    AuditLog,
    LLMError,
    LLMOutputInvalid,
    LLMRefusal,
    LLMRequest,
    Message,
    MockLLMClient,
    extract_json,
    generate_structured,
    make_client,
    redact,
)
from pcbassist.llm.anthropic_client import DEFAULT_MODEL, FALLBACK_BETA, AnthropicClient
from pcbassist.llm.mock import fixture_hash
from pcbassist.schemas.common import Model


class Out(Model):
    name: str
    count: int = Field(ge=0)


def _write(root: Path, stage: str, name: str, body: dict[str, Any]) -> None:
    d = root / stage
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(body))


def _req(key: str, stage: str = "s") -> LLMRequest:
    return LLMRequest(stage=stage, system="sys", messages=(Message("user", key),), fixture_key=key)


# ---------------------------------------------------------------- mock client


def test_mock_exact_keyword_and_default_selection(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "s",
        "iot",
        {"match": ["wifi", "battery"], "responses": ['{"name":"iot","count":1}']},
    )
    _write(
        tmp_path,
        "s",
        "ble",
        {"match": ["ble", "coin cell"], "responses": ['{"name":"ble","count":1}']},
    )
    _write(
        tmp_path,
        "s",
        "exact",
        {"prompt_sha256": fixture_hash("Magic   Prompt"), "responses": ["{}"]},
    )
    _write(tmp_path, "s", "_default", {"responses": ['{"name":"default","count":0}']})
    m = MockLLMClient(tmp_path)
    assert m.mode == "mock" and m.model_name == "mock-llm"
    r = m.complete(_req("WiFi board with battery"))
    assert r.meta["fixture"] == "s/iot" and r.meta["match"].startswith("keyword")
    assert m.complete(_req("BLE beacon on a coin cell")).meta["fixture"] == "s/ble"
    assert m.complete(_req("magic prompt")).meta == {"fixture": "s/exact", "match": "exact"}
    assert m.complete(_req("something unrelated")).meta["fixture"] == "s/_default"
    assert len(m.calls) == 4


def test_mock_sequential_responses_and_errors(tmp_path: Path) -> None:
    _write(tmp_path, "s", "a", {"match": ["x"], "responses": ["one", "two"]})
    _write(tmp_path, "s", "empty", {"match": ["y"], "responses": []})
    m = MockLLMClient(tmp_path)
    assert [m.complete(_req("x")).text for _ in range(3)] == ["one", "two", "two"]
    with pytest.raises(LLMError, match="no responses"):
        m.complete(_req("y"))
    with pytest.raises(LLMError, match="no fixtures for stage"):
        m.complete(_req("x", stage="missing"))
    with pytest.raises(LLMError, match="no fixture"):
        m.complete(_req("zzz"))


def test_mock_serializes_object_responses(tmp_path: Path) -> None:
    _write(tmp_path, "s", "a", {"match": ["x"], "responses": [{"name": "n", "count": 2}]})
    assert json.loads(MockLLMClient(tmp_path).complete(_req("x")).text) == {"name": "n", "count": 2}


# ---------------------------------------------------------- structured output


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1}', '{"a": 1}'),
        ('Here you go:\n```json\n{"a": 1}\n```\nthanks', '{"a": 1}'),
        ('prose {"a": {"b": 2}} trailing', '{"a": {"b": 2}}'),
        ("no json", "no json"),
    ],
)
def test_extract_json(text: str, expected: str) -> None:
    assert extract_json(text) == expected


def test_structured_success_first_try(tmp_path: Path) -> None:
    _write(tmp_path, "s", "a", {"match": ["go"], "responses": ['{"name":"ok","count":3}']})
    audit = AuditLog()
    out = generate_structured(
        MockLLMClient(tmp_path), stage="s", output_model=Out, system="sys", user="go", audit=audit
    )
    assert out == Out(name="ok", count=3)
    assert len(audit.entries) == 1 and audit.entries[0].error is None


def test_structured_retries_with_error_feedback(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "s",
        "a",
        {
            "match": ["go"],
            "responses": [
                "not json at all",
                '{"name":"x","count":-1,"extra":true}',
                '{"name":"fixed","count":1}',
            ],
        },
    )
    client = MockLLMClient(tmp_path)
    audit = AuditLog()
    out = generate_structured(
        client, stage="s", output_model=Out, system="sys", user="go", audit=audit
    )
    assert out.name == "fixed"
    assert [e.attempt for e in audit.entries] == [1, 2, 3]
    assert audit.entries[0].error and audit.entries[1].error and audit.entries[2].error is None
    # The validation errors were fed back to the model on the retry.
    feedback = client.calls[2].messages[-1].content
    assert "count" in feedback and "extra" in feedback
    assert client.calls[2].messages[-2].role == "assistant"
    # Schema is embedded in the system prompt.
    assert '"Out"' in client.calls[0].system or "`Out`" in client.calls[0].system


def test_structured_gives_up_after_max_attempts(tmp_path: Path) -> None:
    _write(tmp_path, "s", "a", {"match": ["go"], "responses": ['{"name": 5}']})
    audit = AuditLog()
    with pytest.raises(LLMOutputInvalid, match="after 3 attempts"):
        generate_structured(
            MockLLMClient(tmp_path), stage="s", output_model=Out, system="s", user="go", audit=audit
        )
    assert len(audit.entries) == 3
    with pytest.raises(ValueError):
        generate_structured(
            MockLLMClient(tmp_path),
            stage="s",
            output_model=Out,
            system="s",
            user="go",
            max_attempts=0,
        )


class _TruncatingClient(MockLLMClient):
    def complete(self, request: LLMRequest):  # type: ignore[no-untyped-def]
        r = super().complete(request)
        if len(self.calls) == 1:
            from dataclasses import replace

            return replace(r, stop_reason="max_tokens")
        return r


def test_structured_treats_truncation_as_invalid(tmp_path: Path) -> None:
    _write(tmp_path, "s", "a", {"match": ["go"], "responses": ['{"name":"a","count":1}']})
    c = _TruncatingClient(tmp_path)
    assert generate_structured(c, stage="s", output_model=Out, system="s", user="go").name == "a"
    assert "truncated" in c.calls[1].messages[-1].content


# ---------------------------------------------------------------- audit log


def test_redaction() -> None:
    key = "sk-ant-api03-" + "A" * 40
    assert key not in redact(f"key={key}")
    assert "[REDACTED]" in redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    assert redact('{"api_key": "supersecretvalue123"}') == '{"api_key": "[REDACTED]"}'
    assert redact("plain text") == "plain text"


def test_audit_writes_redacted_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "sess" / "audit.jsonl"
    log = AuditLog(path)
    log.log(
        "llm_call",
        stage="s",
        prompt="use sk-ant-api03-" + "x" * 30,
        data={"nested": ["x-api-key: abcdefgh12345678"]},
    )
    log.log("review", data={"decision": "approve"})
    entries = AuditLog.read(path)
    assert len(entries) == 2
    raw = path.read_text()
    assert "sk-ant-api03" not in raw and "abcdefgh12345678" not in raw
    assert AuditLog.read(tmp_path / "missing.jsonl") == []


# ----------------------------------------------------------- live client (faked)


class _FakeMessages:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.result, self.exc = result, exc
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return self.result


def _fake_sdk(result: Any = None, exc: Exception | None = None) -> tuple[Any, _FakeMessages]:
    msgs = _FakeMessages(result, exc)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def _resp(text: str, stop: str = "end_turn", model: str = DEFAULT_MODEL) -> Any:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop,
        model=model,
        stop_details=SimpleNamespace(category="cyber"),
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=None),
        _request_id="req_1",
    )


def test_live_client_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PCBASSIST_MODEL", raising=False)
    monkeypatch.delenv("PCBASSIST_LLM_FALLBACKS", raising=False)
    sdk, msgs = _fake_sdk(_resp('{"a":1}'))
    c = AnthropicClient(client=sdk)
    assert c.model_name == "claude-sonnet-5-5" and c.mode == "live"
    r = c.complete(_req("hello"))
    assert (
        r.text == '{"a":1}'
        and r.request_id == "req_1"
        and r.usage == {"input_tokens": 10, "output_tokens": 5}
    )
    assert msgs.kwargs["model"] == "claude-sonnet-5-5"
    assert msgs.kwargs["fallbacks"] == "default" and msgs.kwargs["betas"] == [FALLBACK_BETA]
    assert msgs.kwargs["messages"] == [{"role": "user", "content": "hello"}]
    assert "output_config" not in msgs.kwargs


def test_live_client_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PCBASSIST_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("PCBASSIST_LLM_FALLBACKS", "off")
    monkeypatch.setenv("PCBASSIST_LLM_EFFORT", "high")
    sdk, msgs = _fake_sdk(_resp("{}", model="claude-opus-4-8"))
    r = AnthropicClient(client=sdk).complete(_req("x"))
    assert msgs.kwargs["model"] == "claude-opus-5-5"
    assert "fallbacks" not in msgs.kwargs and msgs.kwargs["output_config"] == {"effort": "high"}
    assert r.meta["served_by"] == "claude-opus-4-8"


def test_live_client_refusal() -> None:
    sdk, _ = _fake_sdk(_resp("", stop="refusal"))
    with pytest.raises(LLMRefusal, match="cyber"):
        AnthropicClient(client=sdk).complete(_req("x"))


def _status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(code, request=req), body=None)


@pytest.mark.parametrize(
    "exc,match,retryable",
    [
        (lambda: _status_error(anthropic.AuthenticationError, 401), "ANTHROPIC_API_KEY", False),
        (lambda: _status_error(anthropic.NotFoundError, 404), "PCBASSIST_MODEL", False),
        (lambda: _status_error(anthropic.BadRequestError, 400), "rejected", False),
        (lambda: _status_error(anthropic.RateLimitError, 429), "Rate limited", True),
        (lambda: _status_error(anthropic.InternalServerError, 500), "error 500", True),
        (
            lambda: anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
            "reach",
            True,
        ),
    ],
)
def test_live_client_error_mapping(exc: Any, match: str, retryable: bool) -> None:
    sdk, _ = _fake_sdk(exc=exc())
    with pytest.raises(LLMError, match=match) as info:
        AnthropicClient(client=sdk).complete(_req("x"))
    assert info.value.retryable is retryable
    assert "sk-ant" not in str(info.value)


def test_make_client_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make_client(mock=True).mode == "mock"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    assert make_client(mock=False, model="claude-sonnet-5-5").model_name == "claude-sonnet-5-5"


def test_recording_client_writes_fixtures(tmp_path: Path) -> None:
    from pcbassist.llm.recording import RecordingClient

    root = tmp_path / "fx"
    _write(root, "stage", "_default", {"responses": ['{"name": "a", "count": 1}']})
    rec = RecordingClient(MockLLMClient(root), tmp_path / "rec", "demo")
    assert rec.mode == "mock" and rec.model_name == "mock-llm"
    for _ in range(2):
        generate_structured(rec, stage="stage", output_model=Out, system="s", user="u")
    doc = json.loads((tmp_path / "rec" / "stage" / "demo.json").read_text())
    assert len(doc["responses"]) == 2 and doc["prompt_sha256"] == fixture_hash("u")
    assert "Recorded" in doc["_note"]


class _Stream:
    def __init__(self, resp: Any, sink: dict[str, Any], kwargs: dict[str, Any]) -> None:
        self.resp = resp
        sink.update(kwargs)

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self.resp


def test_live_client_tool_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcbassist.llm.anthropic_client import AnthropicClient
    from pcbassist.llm.base import ToolRequest, ToolSpec

    sent: dict[str, Any] = {}
    tool_use = SimpleNamespace(
        type="tool_use", id="tu_1", name="symbol_pins", input={"lib_id": "X:Y"}
    )
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="looking up"), tool_use],
        model="claude-sonnet-5-5",
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=7, output_tokens=3, cache_read_input_tokens=5),
    )
    msgs = SimpleNamespace(stream=lambda **kw: _Stream(resp, sent, kw))
    client = AnthropicClient(client=SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
    turn = client.complete_tools(
        ToolRequest(
            stage="parts",
            system="sys",
            messages=({"role": "user", "content": "go"},),
            tools=(ToolSpec("symbol_pins", "pins", {"type": "object"}),),
        )
    )
    assert turn.calls[0].name == "symbol_pins" and turn.calls[0].input == {"lib_id": "X:Y"}
    assert turn.content is resp.content and turn.text == "looking up"
    assert turn.usage["cache_read_input_tokens"] == 5
    assert sent["tools"][0]["input_schema"] == {"type": "object"}
    assert sent["cache_control"] == {"type": "ephemeral"} and sent["fallbacks"] == "default"


def test_recording_client_records_tool_turns(tmp_path: Path) -> None:
    from pcbassist.llm.base import ToolCall, ToolRequest, ToolTurn
    from pcbassist.llm.recording import RecordingClient

    class Inner:
        mode = "live"
        model_name = "m"

        def __init__(self) -> None:
            self.n = 0

        def complete_tools(self, request: ToolRequest) -> ToolTurn:
            self.n += 1
            call = (
                ToolCall("a", "search_symbols", {"query": "x"})
                if self.n == 1
                else ToolCall("b", "submit", {"json": "{}"})
            )
            return ToolTurn(text="", calls=(call,), content=[], model="m")

    rec = RecordingClient(Inner(), tmp_path, "ex")  # type: ignore[arg-type]
    req = ToolRequest(stage="parts", system="", messages=(), tools=(), fixture_key="k")
    rec.complete_tools(req)
    rec.complete_tools(req)
    doc = json.loads((tmp_path / "parts" / "ex.json").read_text())
    assert doc["responses"] == [
        {"tool_calls": [{"name": "search_symbols", "input": {"query": "x"}}]},
        "{}",
    ]
