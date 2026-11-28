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
11. **Placement.** Deterministic, in this order: mounting holes at the corners; then edge
    items flush against their edge (antenna modules on the top edge starting from the
    right, power inputs on the left, field terminals on the right, battery and headers on
    the bottom); then functional parts, largest first, each at the free spot nearest its
    zone's preferred point (power near the input, drivers near the terminals, sensors far
    from the regulators); then support parts at the free spot nearest the pad they serve.
    Courtyards are decomposed into rectangles, so T-shaped module courtyards with antenna
    clearance do not waste board area. A 2 mm "halo" around each IC is kept free while big
    parts are placed. Positions are on a 0.5 mm grid. (The plan originally said shelf
    packing; nearest-free-spot handled the edge and proximity constraints better.)
12. **Web UI.** FastAPI serves a single static HTML/JS page (no build step).
14. **Schematic.** A flat `.kicad_sch` of *generated* box symbols built from the same pin
    tables, with a wire stub and a local net label on every pin and NC flags on the rest.
    Embedding the KiCad 10 library symbols would risk format errors in a KiCad 8 file. The
    test suite has KiCad export a netlist from the schematic and compares it with the
    plan's netlist.
15. **Version semantics.** A version's design content never changes once saved. Comments,
    approvals, locks and check results update that version in place. Edits and refinements
    create version N+1 with `parent_version = N`. `Session.save` enforces this.
16. **Feedback routing.** `refine` picks the restart stage with deterministic keyword
    rules, so a reviewer can predict what will change; `--stage` overrides it. Board size
    and layer-count directives are applied without the LLM.
17. **KiCad DRC classification.** Unrouted connections, silkscreen findings and missing
    library tables are expected on an unrouted auto-placed board, so they are INFO.
    Errors entirely inside one library footprint (module thermal vias, USB-C NPTH
    clearance) are WARNING, with the footprint named.
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
- Battery life = 0.8 x capacity / (duty-weighted active + sleep current). Indicator LEDs
  count toward peak current but not the average (their on-time is application-specific).
  Regulators draw their quiescent current all the time. Switchers are 85 % efficient.
- Coin-cell peak current: up to 1.5x the rated pulse current is a warning (it can be
  bridged with bulk capacitance); beyond that it is an error.
- Net-class clearance equals the fab minimum (0.15 mm), because fine-pitch library
  footprints have 0.15 mm pad gaps.
- Intent engine (default since M8): a part outside the curated library is accepted only
  with an exact KiCad symbol and footprint whose pins and pads match; it is wired from the
  KiCad pin table and flagged `LIB-KICAD-ONLY`. Synth engine: an MPN that is not in the
  library is kept, flagged `verified=false`, confidence ≤ 0.3, and excluded from the netlist.

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
8. **Intent engine** (copperhead-style): LLM-authored parts and netlist over the full KiCad
   library, verified and repaired in tool loops.

## 5. Risks / known limitations

- Live-LLM path is untested until an API key is supplied; mock fixtures are hand-written.
- KiCad symbol pin tables are trusted as the pin source; they are community-reviewed but
  still require datasheet verification before fabrication (stated in the report).
- Electrical limits in `parts_curated.toml` are curated from datasheet knowledge and each
  entry carries `verified_by` provenance notes; anything uncertain is marked in `notes`.
- No routing, single-sided placement, 0° rotation. Edge connectors still need rotating so
  their mating face points off the board.
- Synth engine: subcircuit rules cover the parts the examples use plus their close
  relatives. Other parts get power and ground, and their remaining pins become explicit NCs
  with a SYNTH-INCOMPLETE warning, never silent guesses.
- Intent engine: correctness is bounded by the validators (parts exist, pins exist, every
  pin accounted for, ERC/power rules pass). A circuit can pass them and still be a poor
  design; ratings of KiCad-only parts are model claims. Both are flagged for review.

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

### M2 - Core pipeline (done 2026-11-26)
- **Works:** requirements -> architecture -> components (LLM, schema-validated) -> netlist
  (deterministic synthesizer, `pipeline/synth.py`). The synthesizer builds the power tree
  (USB/DC/battery sources, fuse + series diode + TVS chains, MCP73831 charger, fixed LDOs,
  LM2596-5.0 buck), wires ESP32/nRF52 modules (decoupling, EN RC, strapping pull-ups,
  boot/reset buttons, native USB, SWD, UART), I2C devices (pull-ups, address pins,
  CS-high for I2C mode, address assignment and conflicts), LEDs, buttons, relays (FET,
  flyback, gate resistors), DRV8871, battery-sense divider and headers. Every pin of every
  verified part ends up on a net or as an explicit NC with a reason. Support-part keys are
  stable, so ref designators survive refinements. Hand-authored fixtures cover 4 examples
  and 1 refinement.
- **Library fixes:** TVS `A1`/`A2` pins are no longer tagged as I2C address pins, and
  LIS3DH `SPC` is tagged SCL.
### M3 - Constraints, placement, routing (done 2026-11-26)
- **Works:** rails with budgets; IPC-2221 power width (with 1.25x margin, minimum 0.4 mm);
  Default/Power/Ground/USB net classes from the fab profile; decoupling requirements; USB
  90 Ω differential target; outline from the requirements or auto-sized; corner mounting
  holes; connector edge assignment; placement as in decision 11, with RF keepouts and
  zones; stackup and prioritised routing guidance.
- **Changed:** the IoT example board is 50 x 35 mm, because the C3 module's 28.5 mm-wide
  courtyard (including antenna clearance) plus USB-C and JST did not fit in 45 x 30. The
  BLE board is 40 x 30 mm because placement is single-sided (stated as an assumption).
### M4 - Checks (done 2026-11-26)
- **Works:** 17 ERC rules, 8 power rules, 14 DFM/placement rules, library rules, and the
  KiCad DRC parser. Each ERC rule has a mutation test. The flawed example FAILs on 12
  errors covering every intended flaw; the three good examples are PASS_WITH_WARNINGS
  with realistic warnings.
### M5 - Exporters (done 2026-11-26)
- **Works:** `.net` (KiCad "E" format), `.kicad_pro` (net classes, rules from the fab
  profile, disclaimer variable), `.kicad_pcb` (vendored footprints with nets, rounded
  outline, unfilled GND pour on B.Cu, the modules' own keepout zones, deterministic
  UUIDs), `.kicad_sch` (decision 14), BOM CSV, `report.md`, and DRC via `kicad-cli`.
- **Verified with real KiCad 8.0.9 (Docker):** every board loads, DRC runs and finds no
  courtyard overlaps, footprint zones land at the right board coordinates (checked
  through the pcbnew Python API), and KiCad's netlist exported from our schematic equals
  the plan netlist for all examples.
### M6 - Review and iteration (done 2026-11-26)
- **Works:** sessions with versions (decision 15); comment, resolve, approve, reject,
  edit, lock and unlock on components, nets, rails, net classes, placements, constraints,
  requirements and the whole plan. Plan approval is refused while checks FAIL unless
  forced. `refine` does partial regeneration (decision 16), carrying locks, edits,
  approvals and rejections over. `diff` (text or JSON); CLI `review` subcommands;
  FastAPI REST API and a static review page.
- **Fix:** the SQLite library connection is now shareable across API worker threads,
  guarded by a lock.
### M7 - Examples, docs, Docker, demo (done 2026-11-26)
- **Works:** `examples/*/out` committed: full sessions with KiCad DRC, plus the IoT v2
  refinement and its diff. README. A `Dockerfile` on `kicad/kicad:8.0.9` (built and run
  locally: generate with in-container DRC works). `scripts/kicad-cli-docker`;
  `make demo`, `serve-demo`, `examples`, `test-kicad`, `docker-demo`. CI gains a KiCad
  container job.
- **Tested:** 214 tests (+2 real-KiCad tests that run when kicad-cli is available), 92 %
  line coverage, ruff clean, mypy strict on the core modules.
- **Still untested:** the live Anthropic path against the real API (no key here).

### M8 - Intent engine, after copperhead (done 2026-11-28)
Studied [copperhead](https://github.com/copperheadhq/copperhead) (Apache-2.0; ideas adapted,
no code copied). What makes it accurate, and what we took:

| copperhead | pcbassist |
|---|---|
| Parts manifest (`parts.intent.json`) validated by `draft_bom` against the installed KiCad libraries: lib_id resolves, footprint resolves, symbol pins are footprint pads, value is not prose, one entry per refdes, MPN for active parts, substitutions recorded | `PartsManifest` + `resolve_manifest` with the same rules, plus curated-library parts (verified ratings), locked/rejected parts and `blockers` |
| Passive shorthand (`package: "0603"` -> stock symbol + footprint) | same table (`PASSIVES`) |
| Netlist-intent IR (`REF.PIN` endpoints, `noConnect`), validated before drafting: pin exists (lists real pins), pin in one net, power-token collisions | `NetlistIntent` + `validate_intent`: same, plus "every pin in a net or a no-connect with a reason", NC power/ground refused, rail voltages, pin-name -> number hints |
| Pin dossier of every part's real pins injected into the schematic prompt | `pin_dossier` in the netlist prompt |
| `search_symbols` / `symbol_pins` / `check_footprints` tools; "installed as" suggestions on a miss | same tools over a bundled index of all 21,700 KiCad 8.0.9 symbols and 14,193 footprints |
| Verification-gated loop: nothing is done until ERC/DRC agree; findings fed back to the model | tool loop with `submit`; ERC/power ERRORs fed back; part-level ones loop back to the parts stage |
| Refusal with blockers when a constraint cannot be met | `PartsManifest.blockers` -> `SEL-BLOCKER` errors |

Not taken (out of scope for a prompt-to-plan tool): editing existing KiCad repos, the
schematic drawing engine and legibility scoring, SPICE gate, grounded review bundle, MCP
server, multiple model providers.

Decisions:
- **D18. Default engine = intent.** The LLM now writes pin numbers, but only from tables
  the tool gives it, and every one is checked. The synthesizer stays as `--engine synth`
  and is what the mock parts/netlist fixtures are derived from.
- **D19. Bundled KiCad index** (`data/kicad/*.json.gz`, 3.6 MB) instead of requiring a
  KiCad install: search, pins and pad checks work everywhere (CI, Docker, laptops).
  Boards embed real footprint files when a library tree exists (`make kicad-libs`),
  otherwise footprints rebuilt from pads + courtyard.
- **D20. ERC feedback split.** Wiring problems (unconnected, shorts, driver conflicts,
  ground pins, supply range) go back to the netlist stage; problems that need a part
  (decoupling, pull-ups, CC resistors, flyback, LED resistor, capacity) re-run the parts
  stage, at most 2 extra rounds, stopping early if the manifest does not change.
- **D21. Pin-based ERC on KiCad-only parts.** `Component.pins_known` (curated or KiCad
  symbol) gates pin rules, layout and export; `verified` (curated ratings) still gates
  nothing silently: unverified ratings are flagged instead.
- **D22. Submission budget:** 5 submissions and 30 turns per tool stage; the best
  schema-valid submission is kept and its open problems become error findings.

- **Works:** five examples through the intent engine in mock mode; four match the synth
  engine's netlist exactly (tested), `motor_current_monitor` adds an INA226 that only the
  intent engine can wire. Real KiCad 8.0.9 (Docker): all boards load and DRC runs; the
  netlist KiCad extracts from our schematic matches the plan for iot, relay and the INA226
  board. `pcbassist kicad search|pins|footprint`. `--engine`, recording of tool turns.
- **Tested:** 250 tests (+3 real-KiCad tests, passing via Docker), 86 % coverage, ruff
  clean, mypy strict.
- **Still untested:** the tool loop against the real API (no key here).

