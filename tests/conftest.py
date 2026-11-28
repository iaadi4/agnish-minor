from __future__ import annotations

import os

# hermetic: footprints come from the vendored set and the bundled index, never ~/.cache
os.environ.setdefault("PCBASSIST_KICAD_FOOTPRINT_DIR", "none")

from functools import cache
from pathlib import Path

import pytest

from pcbassist.library import ComponentLibrary
from pcbassist.llm import MockLLMClient
from pcbassist.pipeline import generate_plan
from pcbassist.schemas import DesignPlan

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = [
    "iot_env_sensor",
    "ble_coin_tag",
    "relay_motor_12v",
    "flawed_coin_wifi",
    "motor_current_monitor",
]


def example_prompt(name: str) -> str:
    return (ROOT / "examples" / name / "prompt.txt").read_text().strip()


@cache
def _lib() -> ComponentLibrary:
    return ComponentLibrary.load()


@cache
def _plan(name: str) -> DesignPlan:
    return generate_plan(example_prompt(name), client=MockLLMClient(), lib=_lib())


@pytest.fixture(scope="session")
def library() -> ComponentLibrary:
    return _lib()


@pytest.fixture(scope="session")
def plans() -> dict[str, DesignPlan]:
    return {n: _plan(n) for n in EXAMPLES}


@pytest.fixture
def plan_copy(plans: dict[str, DesignPlan]):  # type: ignore[no-untyped-def]
    """Deep copy of an example plan that a test may mutate."""

    def get(name: str) -> DesignPlan:
        return plans[name].model_copy(deep=True)

    return get
