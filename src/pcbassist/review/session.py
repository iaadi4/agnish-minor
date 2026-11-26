"""Session storage: one directory per design.

Layout::

    <session>/
      plan.json          latest version (copy)
      plan.v<N>.json     one file per version; design content of a version never changes
                         (only its review metadata: comments, decisions, approval status)
      audit.jsonl        LLM prompts/responses, review decisions (API keys redacted)
      v<N>/              exported files for version N (KiCad, BOM, report)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..llm import AuditLog
from ..schemas import DesignPlan

_V = re.compile(r"^plan\.v(\d+)\.json$")


class SessionError(RuntimeError):
    pass


class Session:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def latest_path(self) -> Path:
        return self.root / "plan.json"

    @property
    def audit_path(self) -> Path:
        return self.root / "audit.jsonl"

    def exists(self) -> bool:
        return self.latest_path.exists()

    def audit(self) -> AuditLog:
        return AuditLog(self.audit_path)

    def versions(self) -> list[int]:
        if not self.root.is_dir():
            return []
        out = []
        for p in self.root.iterdir():
            m = _V.match(p.name)
            if m:
                out.append(int(m.group(1)))
        return sorted(out)

    def next_version(self) -> int:
        vs = self.versions()
        return (vs[-1] + 1) if vs else 1

    def version_path(self, version: int) -> Path:
        return self.root / f"plan.v{version}.json"

    def export_dir(self, version: int) -> Path:
        return self.root / f"v{version}"

    def save(self, plan: DesignPlan) -> Path:
        """Write ``plan.v<N>.json`` and ``plan.json``.

        Refuses to change the design content of an existing version.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.version_path(plan.version)
        if path.exists():
            old = DesignPlan.model_validate_json(path.read_text())
            if _design_content(old) != _design_content(plan):
                raise SessionError(
                    f"version {plan.version} already exists with different design content; "
                    "save the change as a new version"
                )
        text = plan.model_dump_json(indent=2)
        path.write_text(text + "\n")
        if plan.version >= max(self.versions() or [0]):
            self.latest_path.write_text(text + "\n")
        return path

    def load(self, version: int | None = None) -> DesignPlan:
        path = self.latest_path if version is None else self.version_path(version)
        if not path.exists():
            raise SessionError(f"no plan at {path}")
        return DesignPlan.model_validate_json(path.read_text())


#: Fields that carry review metadata only; everything else is design content.
REVIEW_FIELDS = {"comments", "decisions", "review_status", "check_summary", "findings"}


def _design_content(plan: DesignPlan) -> str:
    data = plan.model_dump(mode="json", exclude=REVIEW_FIELDS)
    for c in data.get("components", []):
        c.pop("status", None)
        c.pop("locked", None)
    if data.get("netlist"):
        for n in data["netlist"]["nets"]:
            n.pop("status", None)
            n.pop("locked", None)
    if data.get("constraints"):
        data["constraints"].pop("status", None)
        data["constraints"].pop("locked", None)
    return json.dumps(data, sort_keys=True)


def resolve_plan_path(p: Path) -> tuple[Session, int | None]:
    """Accept a session directory, ``plan.json`` or ``plan.v<N>.json``."""
    p = Path(p)
    if p.is_dir():
        return Session(p), None
    m = _V.match(p.name)
    if m:
        return Session(p.parent), int(m.group(1))
    return Session(p.parent), None
