"""Design-check findings and the aggregated report status."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .common import FindingCategory, FindingSource, Model, Severity


class Finding(Model):
    id: str = Field(description="Stable id: '<rule>:<subject>', e.g. 'ERC-DECOUPLE:U1.3'")
    rule: str = Field(description="Rule id, e.g. 'ERC-DECOUPLE'")
    severity: Severity
    category: FindingCategory
    message: str
    refs: list[str] = Field(default_factory=list)
    nets: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None
    source: FindingSource = FindingSource.INTERNAL


class CheckStatus(StrEnum):
    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    FAIL = "FAIL"


class CheckSummary(Model):
    status: CheckStatus
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    kicad_drc_ran: bool = False
    kicad_drc_note: str | None = None

    @classmethod
    def from_findings(
        cls, findings: list[Finding], kicad_drc_ran: bool = False, note: str | None = None
    ) -> CheckSummary:
        e = sum(1 for f in findings if f.severity == Severity.ERROR)
        w = sum(1 for f in findings if f.severity == Severity.WARNING)
        i = sum(1 for f in findings if f.severity == Severity.INFO)
        status = (
            CheckStatus.FAIL if e else CheckStatus.PASS_WITH_WARNINGS if w else CheckStatus.PASS
        )
        return cls(
            status=status,
            errors=e,
            warnings=w,
            infos=i,
            kicad_drc_ran=kicad_drc_ran,
            kicad_drc_note=note,
        )
