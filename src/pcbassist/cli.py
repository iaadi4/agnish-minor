"""`pcbassist` command-line entry point (Typer)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .library import ComponentLibrary, FootprintResolver, SearchQuery
from .schemas import DISCLAIMER, export_json_schemas

app = typer.Typer(
    name="pcbassist",
    help="Prompt-driven PCB design assistant. " + DISCLAIMER,
    no_args_is_help=True,
    add_completion=False,
)
library_app = typer.Typer(help="Inspect the curated component library.", no_args_is_help=True)
schema_app = typer.Typer(help="JSON Schemas of all data models.", no_args_is_help=True)
app.add_typer(library_app, name="library")
app.add_typer(schema_app, name="schema")

MockOpt = Annotated[
    bool, typer.Option("--mock-llm", help="Use recorded fixtures instead of the API")
]


def _not_yet(feature: str, milestone: int) -> None:
    typer.secho(
        f"'{feature}' is not implemented yet (planned for milestone {milestone}).",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def generate(
    prompt: Annotated[str, typer.Argument(help="Natural-language hardware requirement")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output/session directory")] = Path(
        "out"
    ),
    mock_llm: MockOpt = False,
    model: Annotated[str | None, typer.Option(help="Override PCBASSIST_MODEL")] = None,
) -> None:
    """Generate a full design plan and output files from a prompt."""
    _not_yet("generate", 2)


@app.command()
def check(plan: Annotated[Path, typer.Argument(help="plan.json")]) -> None:
    """Run ERC / power / DFM (and KiCad DRC if available) on a plan."""
    _not_yet("check", 4)


@app.command()
def refine(
    plan: Annotated[Path, typer.Argument(help="plan.json")],
    feedback: Annotated[str, typer.Option("--feedback", "-f")],
    mock_llm: MockOpt = False,
) -> None:
    """Regenerate only the stages affected by feedback and show a diff."""
    _not_yet("refine", 6)


@app.command()
def export(
    plan: Annotated[Path, typer.Argument(help="plan.json")],
    format: Annotated[str, typer.Option("--format", help="kicad|bom|report|all")] = "all",
) -> None:
    """Export KiCad files, BOM CSV and/or report.md."""
    _not_yet("export", 5)


@app.command()
def diff(a: Path, b: Path) -> None:
    """Diff two plan versions."""
    _not_yet("diff", 6)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the local review UI and REST API."""
    _not_yet("serve", 6)


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
        typer.secho(f"not in library: {mpn}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
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
def schema_export(
    out: Annotated[Path, typer.Option("--out", "-o")] = Path("schemas"),
) -> None:
    """Write <Model>.schema.json for every data model."""
    for p in export_json_schemas(out):
        typer.echo(str(p))


@schema_app.command("show")
def schema_show(name: str) -> None:
    """Print the JSON Schema of one model (e.g. Requirements, DesignPlan)."""
    from .schemas import EXPORTED_MODELS

    model = EXPORTED_MODELS.get(name)
    if model is None:
        typer.secho(
            f"unknown model; choose from: {', '.join(EXPORTED_MODELS)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(json.dumps(model.model_json_schema(), indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()
