from __future__ import annotations

import pytest

from pcbassist.library import ComponentLibrary


@pytest.fixture(scope="session")
def library() -> ComponentLibrary:
    return ComponentLibrary.load()
