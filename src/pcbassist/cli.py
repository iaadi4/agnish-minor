"""`pcbassist` command-line entry point (Typer)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from . import __version__
from .library import ComponentLibrary, FootprintResolver, SearchQuery
from .schemas import DISCLAIMER, DesignPlan, ItemRef, Severity, export_json_schemas

app = typer.Typer(
    name="pcbassist",
    help="Prompt-driven PCB design assistant. " + DISCLAIMER,
    no_args_is_help=True,
    add_completion=False,
)
library_app = typer.Typer(help="Inspect the curated component library.", no_args_is_help=True)
schema_app = typer.Typer(help="JSON Schemas of all data models.", no_args_is_help=True)
review_app = typer.Typer(
    help="Comment on, approve, reject, edit or lock plan items.", no_args_is_help=True
)
app.add_typer(library_app, name="library")
app.add_typer(schema_app, name="schema")
app.add_typer(review_app, name="review")
kicad_app = typer.Typer(
    help="Look up stock KiCad 8 symbols and footprints (the bundled index the LLM uses).",
    no_args_is_help=True,
)
app.add_typer(kicad_app, name="kicad")

MockOpt = Annotated[
    bool, typer.Option("--mock-llm", help="Use bundled fixtures instead of the API")
]
ModelOpt = Annotated[str | None, typer.Option("--model", help="Override PCBASSIST_MODEL")]
EngineOpt = Annotated[
    str,
    typer.Option(
        "--engine",
        help="intent: the LLM picks real KiCad parts and writes the netlist, verified and "
        "repaired by deterministic checks (default); synth: curated parts + rule templates",
    ),
]
NoDrcOpt = Annotated[bool, typer.Option("--no-drc", help="Skip KiCad DRC even if kicad-cli exists")]
PlanArg = Annotated[Path, typer.Argument(help="Session directory, plan.json or plan.v<N>.json")]
TargetOpt = Annotated[
    str, typer.Option("--target", "-t", help="kind:id, e.g. component:U3, net:GND, plan:*")
]


def _fail(msg: str, code: int = 1) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def _client(
    mock: bool, model: str | None, record: Path | None = None, name: str = "recorded"
) -> Any:
    from .llm import LLMError, make_client
    from .llm.recording import RecordingClient

    try:
        client = make_client(mock=mock, model=model)
    except LLMError as e:
        _fail(str(e), 2)
    if record is not None and not mock:
        client = RecordingClient(client, record, name)
    return client


def _print_summary(plan: DesignPlan, out: Path | None = None) -> None:
    cs = plan.check_summary
    color = {
        "PASS": typer.colors.GREEN,
        "PASS_WITH_WARNINGS": typer.colors.YELLOW,
        "FAIL": typer.colors.RED,
    }
    status = cs.status.value if cs else "UNCHECKED"
    typer.secho(f"\n{DISCLAIMER}\n", fg=typer.colors.MAGENTA, err=True)
    typer.echo(
        f"Plan v{plan.version}: {len(plan.components)} components, "
        f"{len(plan.netlist.nets) if plan.netlist else 0} nets"
    )
    typer.secho(
        f"Checks: {status}" + (f" ({cs.errors} errors, {cs.warnings} warnings)" if cs else ""),
        fg=color.get(status, typer.colors.WHITE),
    )
    if cs and cs.kicad_drc_note:
        typer.echo(f"KiCad DRC: {cs.kicad_drc_note}")
    for f in plan.findings:
        if f.severity == Severity.ERROR:
            typer.secho(f"  ERROR {f.id}: {f.message}", fg=typer.colors.RED)
    if out is not None:
        typer.echo(f"Output: {out}")


def _parse_target(t: str) -> ItemRef:
    kind, sep, ident = t.partition(":")
    if not sep:
        _fail("target must look like kind:id, e.g. component:U3")
    try:
        return ItemRef(kind=kind, id=ident or "*")
    except Exception as e:  # pydantic validation error
        _fail(f"bad target '{t}': {e}")
        raise  # pragma: no cover


def _parse_value(v: str) -> Any:
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


def _export(session: Any, plan: DesignPlan, drc: bool) -> Path:
    from .export import export_plan

    out: Path = session.export_dir(plan.version)
    export_plan(plan, out, drc=drc)
    session.save(plan)
    return out


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def generate(
    prompt: Annotated[
        str | None, typer.Argument(help="Natural-language hardware requirement")
    ] = None,
    prompt_file: Annotated[
        Path | None, typer.Option("--prompt-file", "-f", help="Read the prompt from a file")
    ] = None,
    out: Annotated[Path, typer.Option("--out", "-o", help="Session directory")] = Path("out"),
    mock_llm: MockOpt = False,
    model: ModelOpt = None,
    fab_profile: Annotated[str | None, typer.Option(help="generic_2layer | generic_4layer")] = None,
    no_drc: NoDrcOpt = False,
    engine: EngineOpt = "intent",
    record_fixtures: Annotated[
        Path | None, typer.Option(help="Save live LLM answers as fixtures here")
    ] = None,
) -> None:
    """Generate a full design plan and all output files from a prompt."""
    from .llm import LLMError
    from .pipeline import PipelineOptions, generate_plan
    from .review import Session

    if prompt_file is not None:
        prompt = prompt_file.read_text().strip()
    if not prompt:
        _fail("give a prompt argument or --prompt-file")
    assert prompt is not None
    if engine not in ("intent", "synth"):
        _fail("--engine must be 'intent' or 'synth'")
    session = Session(out)
    if session.exists():
        _fail(f"{out} already holds a session; use `pcbassist refine` or another --out")
    client = _client(mock_llm, model, record_fixtures, out.name)
    try:
        plan = generate_plan(
            prompt,
            client=client,
            lib=ComponentLibrary.load(),
            audit=session.audit(),
            options=PipelineOptions(fab_profile=fab_profile, engine=engine),
        )
    except LLMError as e:
        _fail(f"LLM stage '{e.stage}' failed: {e}", 2)
    session.save(plan)
    outdir = _export(session, plan, drc=not no_drc)
    _print_summary(plan, outdir)


@app.command()
def check(
    plan_path: PlanArg,
    drc: Annotated[bool, typer.Option("--drc", help="Re-export and run KiCad DRC")] = False,
) -> None:
    """Re-run ERC / power / DFM (and KiCad DRC with --drc) on a plan. Exit 1 on FAIL."""
    from .checks import run_checks
    from .review import resolve_plan_path

    session, ver = resolve_plan_path(plan_path)
    plan = session.load(ver)
    if drc:
        _export(session, plan, drc=True)
    else:
        run_checks(plan)
        session.save(plan)
    for f in plan.findings:
        typer.echo(f"{f.severity.value.upper():7} {f.id}: {f.message}")
    _print_summary(plan)
    if plan.check_summary and plan.check_summary.status.value == "FAIL":
        raise typer.Exit(code=1)


@app.command()
def refine(
    plan_path: PlanArg,
    feedback: Annotated[str, typer.Option("--feedback", "-f", help="Reviewer feedback")],
    stage: Annotated[str | None, typer.Option(help="Force the stage to regenerate from")] = None,
    mock_llm: MockOpt = False,
    model: ModelOpt = None,
    no_drc: NoDrcOpt = False,
) -> None:
    """Regenerate only the stages affected by feedback; saves a new version and prints a diff."""
    from .llm import LLMError
    from .review import diff_plans, format_diff, refine_plan, resolve_plan_path

    session, ver = resolve_plan_path(plan_path)
    plan = session.load(ver)
    client = _client(mock_llm, model)
    try:
        new, cls = refine_plan(
            plan,
            feedback,
            client=client,
            lib=ComponentLibrary.load(),
            audit=session.audit(),
            stage=stage,
            version=session.next_version(),
        )
    except LLMError as e:
        _fail(f"LLM stage '{e.stage}' failed: {e}", 2)
    except ValueError as e:
        _fail(str(e))
    typer.echo(f"Regenerated from stage '{cls.stage}' ({cls.reason}).")
    session.save(new)
    outdir = _export(session, new, drc=not no_drc)
    typer.echo(format_diff(diff_plans(plan, new)))
    _print_summary(new, outdir)


@app.command()
def export(
    plan_path: PlanArg,
    format: Annotated[str, typer.Option("--format", help="kicad|bom|report|all")] = "all",
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Output directory")] = None,
    no_drc: NoDrcOpt = False,
) -> None:
    """Export KiCad files, BOM CSV and/or report.md for a plan version."""
    from .export import FORMATS, export_plan
    from .review import resolve_plan_path

    formats = FORMATS if format == "all" else tuple(format.split(","))
    bad = set(formats) - set(FORMATS)
    if bad:
        _fail(f"unknown format(s) {sorted(bad)}; choose from {FORMATS} or all")
    session, ver = resolve_plan_path(plan_path)
    plan = session.load(ver)
    target = out or session.export_dir(plan.version)
    res = export_plan(plan, target, formats=formats, drc=not no_drc)
    if out is None:
        session.save(plan)
    for f in res.files:
        typer.echo(str(f))
    if res.drc is not None:
        typer.echo(f"KiCad DRC: {res.drc.note}")


@app.command()
def diff(
    a: Annotated[Path, typer.Argument(help="First plan file")],
    b: Annotated[Path, typer.Argument(help="Second plan file")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Diff two plan versions (components, nets, rails, outline, placement, findings)."""
    from .review import diff_plans, format_diff

    pa = DesignPlan.model_validate_json(a.read_text())
    pb = DesignPlan.model_validate_json(b.read_text())
    d = diff_plans(pa, pb)
    if as_json:
        typer.echo(json.dumps(d.to_dict(), indent=2, default=str))
    else:
        typer.echo(format_diff(d))


@app.command()
def serve(
    sessions: Annotated[Path, typer.Option(help="Directory holding session folders")] = Path(
        "sessions"
    ),
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Start the local review UI and REST API."""
    import uvicorn

    from .ui.app import create_app

    typer.echo(f"Review UI on http://{host}:{port}  (sessions in {sessions.resolve()})")
    uvicorn.run(create_app(sessions), host=host, port=port)


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def _review_action(
    plan_path: Path,
    target: str,
    decision: str,
    note: str,
    author: str,
    force: bool = False,
    edit: dict[str, Any] | None = None,
) -> None:
    from .checks import run_checks
    from .review import ReviewError, apply_decision, resolve_plan_path

    session, ver = resolve_plan_path(plan_path)
    plan = session.load(ver)
    ref = _parse_target(target)
    if decision == "edit":
        plan = plan.model_copy(deep=True)
        plan.parent_version = plan.version
        plan.version = session.next_version()
    try:
        d = apply_decision(
            plan,
            ref,
            decision,
            author=author,
            note=note,
            edit=edit,
            lib=ComponentLibrary.load(),
            force=force,
        )
    except ReviewError as e:
        _fail(str(e))
    if decision == "edit":
        run_checks(plan)
    session.save(plan)
    session.audit().log(
        "review",
        data={
            "decision": d.decision,
            "target": ref.key(),
            "note": note,
            "author": author,
            "version": str(plan.version),
        },
    )
    typer.echo(f"{d.id}: {decision} {ref.key()} (plan v{plan.version})")
    if decision == "edit":
        typer.echo(
            "Edit saved as a new version. Run `pcbassist refine --stage netlist` to "
            "re-synthesize around it, or `pcbassist export` to regenerate files."
        )
        _print_summary(plan)


@review_app.command("comment")
def review_comment(
    plan_path: PlanArg,
    target: TargetOpt,
    text: Annotated[str, typer.Option("--text", "-m")],
    author: str = "reviewer",
) -> None:
    """Attach a comment to an item."""
    from .review import ReviewError, add_comment, resolve_plan_path

    session, ver = resolve_plan_path(plan_path)
    plan = session.load(ver)
    try:
        c = add_comment(plan, _parse_target(target), text, author)
    except ReviewError as e:
        _fail(str(e))
    session.save(plan)
    session.audit().log("review", data={"comment": c.id, "target": c.target.key(), "text": text})
    typer.echo(f"{c.id} on {c.target.key()}")


@review_app.command("approve")
def review_approve(
    plan_path: PlanArg,
    target: TargetOpt,
    note: str = "",
    author: str = "reviewer",
    force: Annotated[bool, typer.Option(help="Approve a plan even if checks fail")] = False,
) -> None:
    """Approve an item (or plan:* for the whole plan)."""
    _review_action(plan_path, target, "approve", note, author, force)


@review_app.command("reject")
def review_reject(
    plan_path: PlanArg, target: TargetOpt, note: str = "", author: str = "reviewer"
) -> None:
    """Reject an item; rejected parts are avoided on the next refine."""
    _review_action(plan_path, target, "reject", note, author)


@review_app.command("lock")
def review_lock(
    plan_path: PlanArg, target: TargetOpt, note: str = "", author: str = "reviewer"
) -> None:
    """Lock an item so regeneration keeps it unchanged."""
    _review_action(plan_path, target, "lock", note, author)


@review_app.command("unlock")
def review_unlock(
    plan_path: PlanArg, target: TargetOpt, note: str = "", author: str = "reviewer"
) -> None:
    """Unlock an item."""
    _review_action(plan_path, target, "unlock", note, author)


@review_app.command("edit")
def review_edit(
    plan_path: PlanArg,
    target: TargetOpt,
    set_: Annotated[list[str], typer.Option("--set", help="field=value (JSON values allowed)")],
    note: str = "",
    author: str = "reviewer",
) -> None:
    """Edit fields of an item; saved as a new version and re-checked."""
    edit: dict[str, Any] = {}
    for kv in set_:
        k, sep, v = kv.partition("=")
        if not sep:
            _fail(f"--set expects field=value, got '{kv}'")
        edit[k] = _parse_value(v)
    _review_action(plan_path, target, "edit", note, author, edit=edit)


@review_app.command("status")
def review_status(plan_path: PlanArg) -> None:
    """Show review state: item statuses, locks, comments, decisions."""
    from .review import resolve_plan_path, review_summary

    session, ver = resolve_plan_path(plan_path)
    plan = session.load(ver)
    typer.echo(f"Plan v{plan.version} ({plan.review_status.value}); versions: {session.versions()}")
    typer.echo(json.dumps(review_summary(plan)))
    for c in plan.comments:
        typer.echo(f"  {c.id} [{'x' if c.resolved else ' '}] {c.target.key()}: {c.text}")
    for d in plan.decisions:
        typer.echo(f"  {d.id} {d.decision} {d.target.key()} {d.note}")


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------


@library_app.command("list")
def library_list(
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
) -> None:
    """List parts (optionally one category)."""
    lib = ComponentLibrary.load()
    parts = lib.search(SearchQuery(categories=[category] if category else [], limit=10_000))
    for p in parts:
        typer.echo(f"{p.category:<20} {p.mpn:<28} {p.description}")
    typer.echo(f"\n{len(parts)} parts", err=True)


@library_app.command("search")
def library_search(
    keywords: Annotated[list[str], typer.Argument(help="Keywords (AND-ed)")],
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
    supply: Annotated[
        float | None, typer.Option(help="Must operate at this supply voltage")
    ] = None,
) -> None:
    """Keyword/category/supply search."""
    lib = ComponentLibrary.load()
    q = SearchQuery(categories=[category] if category else [], keywords=keywords, supply_v=supply)
    for p in lib.search(q):
        typer.echo(f"{p.category:<20} {p.mpn:<28} {p.description}")


@library_app.command("show")
def library_show(mpn: str) -> None:
    """Print one part as JSON."""
    part = ComponentLibrary.load().get(mpn)
    if part is None:
        _fail(f"not in library: {mpn}")
    assert part is not None
    typer.echo(part.model_dump_json(indent=2))


@library_app.command("validate")
def library_validate() -> None:
    """Validate the seed: schema, footprint names, pin/pad consistency."""
    lib = ComponentLibrary.load()
    resolver = FootprintResolver.detect()
    problems: list[str] = []
    for p in lib.all():
        r = resolver.resolve(p.footprint)
        if not r.resolved:
            problems.append(f"{p.mpn}: footprint {p.footprint} unresolved ({r.suggestion})")
        if p.footprint_info:
            pads = set(p.footprint_info.pad_numbers)
            missing = [pin.number for pin in p.pins if pin.number not in pads]
            if missing:
                problems.append(f"{p.mpn}: pins {missing} have no pad")
        if p.category not in {"mechanical", "test_point"} and not p.pins:
            problems.append(f"{p.mpn}: no pins")
    typer.echo(f"{len(lib)} parts checked against {resolver.source}")
    for msg in problems:
        typer.secho(msg, fg=typer.colors.RED)
    raise typer.Exit(code=1 if problems else 0)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


@schema_app.command("export")
def schema_export(out: Annotated[Path, typer.Option("--out", "-o")] = Path("schemas")) -> None:
    """Write <Model>.schema.json for every data model."""
    for p in export_json_schemas(out):
        typer.echo(str(p))


@schema_app.command("show")
def schema_show(name: str) -> None:
    """Print the JSON Schema of one model (e.g. Requirements, DesignPlan)."""
    from .schemas import EXPORTED_MODELS

    model = EXPORTED_MODELS.get(name)
    if model is None:
        _fail(f"unknown model; choose from: {', '.join(EXPORTED_MODELS)}")
    assert model is not None
    typer.echo(json.dumps(model.model_json_schema(), indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()


@kicad_app.command("search")
def kicad_search(
    query: Annotated[str, typer.Argument(help="Part number or words, e.g. INA226")],
    limit: Annotated[int, typer.Option(help="Maximum results")] = 10,
) -> None:
    """Search every stock KiCad symbol library."""
    from .kicad.symbols import default_symbol_index

    hits = default_symbol_index().search(query, limit=limit)
    if not hits:
        _fail(f'no KiCad symbol matches "{query}"')
    for s in hits:
        typer.echo(f"{s.lib_id:45} {len(s.pins):4} pins  {s.footprint or '-'}")
        if s.description:
            typer.echo(f"{'':45} {s.description[:100]}")


@kicad_app.command("pins")
def kicad_pins(
    lib_id: Annotated[str, typer.Argument(help="Lib:Name, e.g. Sensor_Energy:INA226")],
) -> None:
    """Real pins of a KiCad symbol (number, name, electrical type)."""
    from .kicad.symbols import default_symbol_index, natural_key

    r = default_symbol_index().resolve(lib_id)
    if r.info is None:
        _fail(r.message(lib_id))
    assert r.info is not None
    s = r.info
    typer.echo(f"{s.lib_id}: {s.description}")
    typer.echo(f"default footprint: {s.footprint or '-'}   datasheet: {s.datasheet or '-'}")
    for p in sorted(s.pins, key=lambda p: natural_key(p.number)):
        tags = ",".join(p.functions)
        typer.echo(f"  {p.number:>5}  {p.name:16} {p.type:10} {tags}")


@kicad_app.command("footprint")
def kicad_footprint(
    name: Annotated[str, typer.Argument(help="Lib:Name footprint id, or words to search")],
) -> None:
    """Pads and courtyard of a KiCad footprint (or search footprint names)."""
    from .kicad.fpindex import default_footprint_index
    from .kicad.symbols import natural_key

    idx = default_footprint_index()
    g = idx.geom(name) if ":" in name else None
    if g is None:
        hits = idx.search(name) if ":" not in name else idx.nearest(name)
        if not hits:
            _fail(f"no footprint matches {name}")
        for h in hits:
            typer.echo(h)
        return
    typer.echo(
        f"{g.name}: courtyard {g.width:.2f} x {g.height:.2f} mm, {'SMD' if g.smd else 'THT'}"
    )
    for p in sorted(g.pads, key=lambda p: natural_key(p.number)):
        where = f"({p.x:g}, {p.y:g})"
        typer.echo(f"  {p.number or '(np)':>5}  {p.kind:12} {p.shape:9} {where}  {p.w:g} x {p.h:g}")
