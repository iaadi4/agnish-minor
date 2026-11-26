# PLAN — Prompt-Driven PCB Design Assistant (`pcbassist`)

> Every artifact this tool produces is AI-assisted and **must be reviewed by a qualified
> engineer before fabrication**. That disclaimer is baked into schemas, reports and exports.

## 1. Core principle: LLM proposes, deterministic code builds and verifies

| Concern | Owner |
|---|---|
| Interpreting the prompt, assumptions, open questions | LLM → `Requirements` (validated) |
| Choosing architecture blocks and their links | LLM → `Architecture` (validated) |
| Choosing parts **from library candidates** + rationale | LLM → `ComponentSelection` (validated, cross-checked against the library) |
| Pin-level netlist | **Deterministic** synthesizer (library pin tables + subcircuit rules) |
| Constraints, trace widths, footprints, placement x/y, routing guidance | **Deterministic** (rules + fab profiles); LLM may only add free-text notes |
| ERC / power budget / DFM / DRC | **Deterministic** Python + `kicad-cli` |

The LLM never writes a pin number. Its output only names roles, blocks, buses and
library MPNs; the netlist synthesizer turns that into pins using the KiCad-derived pin
tables. This keeps the "who is the source of truth" question simple: the library and the
rule code are, and every LLM claim is checked against them.

## 2. Key decisions

1. **Location.** Standalone repo at `pcb-assistant/` (sibling of the directory the task
   was started from), its own git history. Layout follows the spec's Section 4.
2. **Tooling.** Python 3.11+ (developed on 3.12), `uv` for env/lockfile, hatchling build,
   ruff (lint + format), mypy `--strict` on `schemas/`, `checks/`, `export/`, `kicad/`,
   `library/`, `llm/`; pytest + pytest-cov.
3. **Schemas.** Pydantic v2, `extra="forbid"` on every model so LLM drift fails validation
   instead of silently passing. Every top-level model exports JSON Schema
   (`pcbassist schema export`), and the LLM prompt embeds the schema of the stage output.
4. **LLM client.** Official `anthropic` SDK. Model from `PCBASSIST_MODEL`, default
   `claude-sonnet-5-5` (the spec asks for the latest Sonnet). Server-side refusal fallback
   (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`) is on by default and can
   be disabled with `PCBASSIST_LLM_FALLBACKS=off`. Structured output strategy: the stage's
   JSON Schema is put in the system prompt; the response is parsed and validated with
   Pydantic; on failure the validation errors are fed back and the call is retried (max 3
   attempts total). SDK-level retries handle 429/5xx/connection errors; our wrapper converts
   remaining failures into a typed `LLMError` with a readable message.
5. **Mock mode.** `--mock-llm` uses `MockLLMClient`, which serves JSON fixtures from
   `fixtures/llm/<stage>/*.json`. Lookup: exact prompt-hash match first, then best keyword
   match (`match` keywords in each fixture). A nearest-match is recorded in the audit trail
   as such. **Honesty note:** no API key was available while building, so fixtures are
   hand-authored in the exact shape the live model is asked to produce; they are not
   recordings. `pcbassist generate --record-fixtures` will capture real responses when a
   key is present.
6. **Component library.** `data/components_seed.json` is *generated* by
   `scripts/build_seed.py` from `data/parts_curated.toml` (hand-curated MPN, manufacturer,
   category, electrical limits, specs) merged with the **official KiCad symbol library**
   (pin numbers, names, electrical types, datasheet URL, default footprint). Pin tables are
   therefore never typed by hand for ICs; each part records `pin_source`. Footprint pad
   numbers are cross-checked against the KiCad footprint file at build time. Datasheet URLs
   come only from KiCad symbol metadata; when absent they stay `null`.
   At runtime the seed is loaded into SQLite (`sqlite3`, stdlib) for filtered search.
7. **Footprint resolver.** Checks `lib:name` against a local KiCad install if found
   (`KICAD*_FOOTPRINT_DIR` env or standard paths), else against a bundled index of the
   KiCad footprint libraries (`data/kicad/footprint_index.json`, built from the KiCad repo
   listing). Unresolved names become warnings.
8. **KiCad files.** Our own small, tested S-expression parser/serializer
   (`pcbassist.kicad.sexpr`) — no hard dependency on SKiDL/pcbnew. Footprint geometry for
   library parts is vendored from KiCad (`data/kicad/footprints/`) so the generated
   `.kicad_pcb` contains real pads and DRC is meaningful.
9. **Storage / review.** Each session is a directory: `plan.v<N>.json` (immutable
   versions), `plan.json` (latest), `audit.jsonl` (LLM prompts/responses, decisions; API
   keys redacted). SQLite is used for the library only — files are easier to diff and
   review, and the spec allows either.
10. **Trace widths.** IPC-2221 external-layer formula `I = 0.048·ΔT^0.44·A^0.725`
    (A in mil², ΔT = 10 °C default, 1 oz copper), with a floor at the fab minimum.
11. **Placement.** Deterministic zone packing: zones are derived from block kinds (RF at a
    board edge with antenna keepout, power near input connector, sensors away from
    regulators), then a shelf-packing algorithm with courtyard-size bounding boxes and
    proximity pulls (decouplers next to their pin). Overlap freedom is asserted in tests.
12. **Web UI.** FastAPI serves a single static HTML/JS page (no build step).
13. **Docker.** Image based on the official KiCad image so `kicad-cli` DRC is reproducible.

## 3. Assumptions (where the spec is silent)

- Default board when the prompt is silent: 2 layers, 1.6 mm FR-4, 1 oz copper, profile
  `generic_2layer` (JLC/PCBWay-class 6/6 mil rules). 4-layer profile provided.
- Default Li-ion: single cell, 3.0–4.2 V, 3.7 V nominal; coin cell: CR2032, 2.0–3.0 V,
  220 mAh nominal; battery-life estimates use stated duty cycles and are labelled estimates.
- "Every pin accounted for": unused MCU GPIOs are explicitly no-connect with reason
  "unused GPIO"; module NC pins are NC with reason "NC per datasheet/KiCad symbol".
- Single schematic sheet. `.kicad_sch` export is attempted as a flat sheet with symbols
  placed on a grid and nets expressed as labels (no wires) — "if feasible" per spec.
- KiCad target: version 8/9 file formats (`kicad-cli` 8+).
- Default design-plan unit is millimetres; currents in mA, sleep currents in µA.
- When an LLM proposes an MPN that is not in the library it is kept, flagged
  `verified=false`, given confidence ≤ 0.3, and excluded from the netlist until a human
  confirms it with a pin table.

## 4. Milestones

1. **Foundation** — scaffold, schemas + JSON Schema export, S-expression module, library
   (seed builder, SQLite, footprint resolver), LLM layer (client, mock, structured retry,
   audit), CLI skeleton, CI.
2. **Core pipeline** — requirements → architecture → components → netlist.
3. **Constraints, footprints, placement, routing guidance.**
4. **Checks** — ERC, power budget, DFM + flawed-design fixture.
5. **Exporters** — `.net`, `.kicad_pro`, `.kicad_pcb`, (`.kicad_sch`), BOM CSV, report.md, DRC.
6. **Review & iteration** — versions, diff, comments/decisions/locks, partial regen, UI.
7. **Examples, docs, Docker, `make demo`.**

## 5. Risks / known limitations (initial)

- Live-LLM path is untested until an API key is supplied; mock fixtures are hand-written.
- KiCad symbol pin tables are trusted as the pin source; they are community-reviewed but
  still require datasheet verification before fabrication (stated in the report).
- Electrical limits in `parts_curated.toml` are curated from datasheet knowledge and each
  entry carries `verified_by` provenance notes; anything uncertain is marked in `notes`.

## 6. Milestone log

### M1 — Foundation (done 2026-11-26)
- **Works:** schemas for every Section 5 model (+ JSON Schema export, `extra="forbid"`);
  S-expression parser/serializer (round-trips real KiCad footprints); 102-part library
  (`parts_curated.toml` → `build_seed.py` → `components_seed.json`, pins from KiCad symbols,
  every pin checked against a real footprint pad, 58 datasheet URLs from KiCad metadata,
  44 left null); SQLite search; footprint resolver (local KiCad or bundled 14,193-name
  index); LLM layer (Anthropic client with fallbacks + typed errors, fixture mock,
  validate-and-retry ×3 with error feedback, redacted JSONL audit); CLI `library`, `schema`,
  `version`; CI (ruff, mypy strict, pytest + coverage gate, library validate).
- **Stubbed:** `generate`, `check`, `refine`, `export`, `diff`, `serve` exit with code 2 and
  name their milestone.
- **Tested:** 116 tests, 98% line coverage. Live API path tested only against a faked SDK
  object (no API key available).
- **Library changes during build:** HDC2080 dropped (KiCad 10 footprint absent from the
  KiCad 8 library); USB connector shield pins remapped `SH→S1`/`SH→6` (symbol/footprint
  numbering differs across KiCad versions); barrel jack switched to the no-switch symbol
  to match the PJ-063AH footprint.
