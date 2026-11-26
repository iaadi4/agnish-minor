"""FastAPI review server: REST API over session directories + a single static page."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from ..checks import run_checks
from ..export import export_plan
from ..library import ComponentLibrary
from ..llm import LLMError, make_client
from ..pipeline import PipelineOptions, generate_plan
from ..review import (
    ReviewError,
    Session,
    SessionError,
    add_comment,
    apply_decision,
    diff_plans,
    format_diff,
    refine_plan,
    resolve_comment,
    review_summary,
)
from ..schemas import DISCLAIMER, DesignPlan, ItemRef

STATIC = Path(__file__).parent / "static"
_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class CreateBody(BaseModel):
    prompt: str = Field(min_length=3)
    name: str | None = None
    mock: bool = True
    fab_profile: str | None = None
    drc: bool = False


class CommentBody(BaseModel):
    target: ItemRef
    text: str = Field(min_length=1)
    author: str = "reviewer"


class DecisionBody(BaseModel):
    target: ItemRef
    decision: str
    note: str = ""
    author: str = "reviewer"
    edit: dict[str, Any] | None = None
    force: bool = False


class RefineBody(BaseModel):
    feedback: str = Field(min_length=3)
    stage: str | None = None
    mock: bool = True
    drc: bool = False


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "design"


def create_app(sessions_root: Path, lib: ComponentLibrary | None = None) -> FastAPI:
    root = Path(sessions_root)
    root.mkdir(parents=True, exist_ok=True)
    library = lib or ComponentLibrary.load()
    app = FastAPI(title="pcbassist review", description=DISCLAIMER)

    def session(sid: str) -> Session:
        if not _ID.match(sid):
            raise HTTPException(400, "bad session id")
        s = Session(root / sid)
        if not s.exists():
            raise HTTPException(404, f"no session {sid}")
        return s

    def load(s: Session, version: int | None = None) -> DesignPlan:
        try:
            return s.load(version)
        except SessionError as e:
            raise HTTPException(404, str(e)) from e

    def client(mock: bool) -> Any:
        try:
            return make_client(mock=mock)
        except LLMError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "disclaimer": DISCLAIMER, "parts": len(library)}

    @app.get("/api/sessions")
    def list_sessions() -> list[dict[str, Any]]:
        out = []
        for d in sorted(root.iterdir()) if root.exists() else []:
            s = Session(d)
            if not s.exists():
                continue
            p = s.load()
            out.append(
                {
                    "id": s.id,
                    "version": p.version,
                    "versions": s.versions(),
                    "summary": p.requirements.summary if p.requirements else p.prompt,
                    "status": p.check_summary.status.value if p.check_summary else None,
                    "review_status": p.review_status.value,
                }
            )
        return out

    @app.post("/api/sessions", status_code=201)
    def create(body: CreateBody) -> dict[str, Any]:
        sid = _slug(body.name or body.prompt)
        base, n = sid, 2
        while (root / sid).exists():
            sid, n = f"{base}-{n}", n + 1
        s = Session(root / sid)
        try:
            plan = generate_plan(
                body.prompt,
                client=client(body.mock),
                lib=library,
                audit=s.audit(),
                options=PipelineOptions(fab_profile=body.fab_profile),
            )
        except LLMError as e:
            raise HTTPException(502, f"LLM stage '{e.stage}' failed: {e}") from e
        s.save(plan)
        export_plan(plan, s.export_dir(plan.version), drc=body.drc)
        s.save(plan)
        return {"id": sid, "version": plan.version}

    @app.get("/api/sessions/{sid}")
    def get_plan(sid: str, version: int | None = None) -> dict[str, Any]:
        s = session(sid)
        plan = load(s, version)
        data = plan.model_dump(mode="json")
        data["_review_summary"] = review_summary(plan)
        data["_versions"] = s.versions()
        data["_files"] = (
            sorted(p.name for p in s.export_dir(plan.version).glob("*"))
            if s.export_dir(plan.version).exists()
            else []
        )
        return data

    @app.post("/api/sessions/{sid}/comments", status_code=201)
    def comment(sid: str, body: CommentBody) -> dict[str, Any]:
        s = session(sid)
        plan = load(s)
        try:
            c = add_comment(plan, body.target, body.text, body.author)
        except ReviewError as e:
            raise HTTPException(400, str(e)) from e
        s.save(plan)
        s.audit().log("review", data={"comment": c.id, "target": c.target.key(), "text": c.text})
        return c.model_dump(mode="json")

    @app.post("/api/sessions/{sid}/comments/{cid}/resolve")
    def resolve(sid: str, cid: str) -> dict[str, Any]:
        s = session(sid)
        plan = load(s)
        try:
            c = resolve_comment(plan, cid)
        except ReviewError as e:
            raise HTTPException(404, str(e)) from e
        s.save(plan)
        return c.model_dump(mode="json")

    @app.post("/api/sessions/{sid}/decisions", status_code=201)
    def decide(sid: str, body: DecisionBody) -> dict[str, Any]:
        s = session(sid)
        plan = load(s)
        if body.decision == "edit":
            plan = plan.model_copy(deep=True)
            plan.parent_version = plan.version
            plan.version = s.next_version()
        try:
            d = apply_decision(
                plan,
                body.target,
                body.decision,
                author=body.author,
                note=body.note,
                edit=body.edit,
                lib=library,
                force=body.force,
            )
        except ReviewError as e:
            raise HTTPException(400, str(e)) from e
        if body.decision == "edit":
            run_checks(plan)
            export_plan(plan, s.export_dir(plan.version), drc=False)
        s.save(plan)
        s.audit().log(
            "review",
            data={
                "decision": d.decision,
                "target": d.target.key(),
                "note": d.note,
                "version": str(plan.version),
            },
        )
        return {"decision": d.model_dump(mode="json"), "version": plan.version}

    @app.post("/api/sessions/{sid}/refine", status_code=201)
    def refine(sid: str, body: RefineBody) -> dict[str, Any]:
        s = session(sid)
        plan = load(s)
        try:
            new, cls = refine_plan(
                plan,
                body.feedback,
                client=client(body.mock),
                lib=library,
                audit=s.audit(),
                stage=body.stage,
                version=s.next_version(),
            )
        except LLMError as e:
            raise HTTPException(502, f"LLM stage '{e.stage}' failed: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        s.save(new)
        export_plan(new, s.export_dir(new.version), drc=body.drc)
        s.save(new)
        d = diff_plans(plan, new)
        return {
            "version": new.version,
            "stage": cls.stage,
            "reason": cls.reason,
            "diff": format_diff(d),
            "diff_data": d.to_dict(),
        }

    @app.get("/api/sessions/{sid}/diff")
    def diff(sid: str, a: int, b: int) -> dict[str, Any]:
        s = session(sid)
        d = diff_plans(load(s, a), load(s, b))
        return {"text": format_diff(d), "data": d.to_dict()}

    @app.get("/api/sessions/{sid}/files/{version}/{name}")
    def file(sid: str, version: int, name: str) -> FileResponse:
        s = session(sid)
        if "/" in name or name.startswith("."):
            raise HTTPException(400, "bad file name")
        path = s.export_dir(version) / name
        if not path.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(path, filename=name)

    @app.get("/api/sessions/{sid}/audit")
    def audit(sid: str) -> list[dict[str, Any]]:
        s = session(sid)
        return [e.model_dump(mode="json") for e in s.audit().read(s.audit_path)]

    return app
