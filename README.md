# pcbassist: prompt-driven PCB design assistant

> **AI-GENERATED DESIGN PLANS ARE NOT VERIFIED FOR FABRICATION.** Everything this tool
> produces must be reviewed and signed off by a qualified electronics engineer before any
> fabrication, assembly or use. The disclaimer is embedded in every output file.

`pcbassist` turns a natural-language hardware requirement into a structured, reviewable
PCB design plan: requirements, architecture, a bill of materials, a pin-level netlist,
electrical and mechanical constraints, a deterministic placement, routing guidance, and
the results of the checks (ERC, power budget, DFM and KiCad DRC). It exports
KiCad 8 files (`.net`, `.kicad_pro`, `.kicad_sch`, `.kicad_pcb`), a BOM CSV and a
`report.md`. A human reviews it through comments, approve/reject/edit/lock decisions and
partial regeneration with a diff, either on the CLI or in a local web UI.

## The core principle: the LLM proposes, deterministic code verifies

| Concern | Owner |
|---|---|
| Interpreting the prompt; assumptions, open questions, conflicts | LLM, output validated against the `Requirements` schema |
| Block architecture | LLM (`Architecture`), coverage checked in code |
| Choosing functional parts **from library candidates** | LLM (`ComponentSelection`), every MPN cross-checked against the library |
| Every pin connection, support passive and value | **Deterministic synthesizer** using pin tables derived from KiCad |
| Constraints, IPC-2221 trace widths, outline, holes, placement, routing guidance | **Deterministic** |
| ERC, power budget, DFM, DRC | **Deterministic** Python plus `kicad-cli` |

The LLM never writes a pin number or a coordinate. It can only pick MPNs offered to it.
A part outside the library is kept but marked `verified=false` with confidence ≤ 0.3,
and it stays out of the netlist. Datasheet URLs come only from KiCad symbol metadata;
when that has none, the URL stays empty instead of being guessed.

## Quick start

```bash
uv sync
# Offline: hand-authored fixtures stand in for the model (see "Mock mode" below)
uv run pcbassist generate -f examples/iot_env_sensor/prompt.txt --out out/iot --mock-llm
# Live: Anthropic API (model from PCBASSIST_MODEL, default claude-sonnet-5-5)
export ANTHROPIC_API_KEY=...
uv run pcbassist generate "USB-C powered CO2 sensor with an e-paper display" --out out/co2
```

`out/iot/` is a **session**:

```
plan.json          latest plan        plan.v1.json, plan.v2.json ...  one file per version
audit.jsonl        every LLM prompt/response and review decision (API keys redacted)
v1/                board.kicad_pcb  board.kicad_sch  board.kicad_pro  board.net
                   bom.csv  report.md  drc.json (when kicad-cli ran)
```

`make demo` generates all four examples plus a refinement into `out/demo/`;
`make serve-demo` opens them in the review UI at http://127.0.0.1:8000.

## Pipeline

```
prompt -> requirements -> architecture -> components -> netlist -> constraints
  (LLM)       (LLM)           (LLM)       (synthesis)     (rules)
       -> placement -> routing -> checks -> exports
           (rules)      (rules)   ERC/power/DFM/library (+ KiCad DRC on export)
```

Each LLM stage puts the JSON Schema of its output model in the system prompt and
validates the reply with Pydantic. Unknown keys are rejected (`extra="forbid"`). On a
validation error the errors are sent back and the model retries, up to 3 attempts in
total. A truncated reply counts as invalid. Refusals and API failures surface as typed
`LLMError`s with readable messages.

## Reviewing and iterating

```bash
pcbassist review comment  out/iot -t component:U3 -m "antenna clearance vs enclosure?"
pcbassist review approve  out/iot -t component:U3
pcbassist review lock     out/iot -t component:U2         # kept verbatim on regeneration
pcbassist review reject   out/iot -t component:D2         # avoided on the next refine
pcbassist review edit     out/iot -t placement:R1 --set x_mm=20 --set y_mm=12   # new version
pcbassist refine out/iot --mock-llm -f "Replace the SHT40 with a BME280 so we also get barometric pressure."
pcbassist diff out/iot/plan.v1.json out/iot/plan.v2.json
pcbassist review approve  out/iot -t 'plan:*'             # refuses while checks FAIL (unless --force)
pcbassist check out/iot                                   # exit 1 on FAIL
```

`refine` picks the earliest stage the feedback affects, using deterministic rules:

| Feedback | Restarts at |
|---|---|
| Names a library part, or "replace / instead / swap / use a..." | components |
| "move / place / closer / rotate..." | placement |
| "route / trace / ground plane / stackup..." | routing |
| "60 x 40 mm", "4 layers", "mounting hole", "fab profile" | constraints |
| Anything else | requirements |

Only that stage and the ones after it are regenerated. Numeric directives (board size,
layer count) are applied directly. Locked and edited parts are carried over and the LLM
is told about them; rejected MPNs are excluded. Approvals carry over to parts that did not
change. A locked net that changes produces an error. Each refinement is saved as a new
version, and `diff` reports what changed: parts, nets, rails, outline, moved parts,
findings and the status change.

`pcbassist serve --sessions out` starts a FastAPI app with a single static page: findings,
components with approve/reject/lock/comment buttons, nets, a board view (outline, zones,
antenna keepout, courtyards), the power tree, refinement, a diff between any two versions,
and file downloads. Its REST API is under `/api/sessions/...`.

## Checks

| Group | Rules (finding id prefix) |
|---|---|
| ERC | `ERC-UNCONNECTED` (every pin on a net or an explicit NC with a reason), `ERC-MULTI-NET`, `ERC-NC-CONNECTED`, `ERC-SINGLE-PIN-NET`, `ERC-DRIVER-CONFLICT`, `ERC-POWER-UNDRIVEN`, `ERC-GROUND-PIN`, `ERC-SUPPLY-RANGE`, `ERC-DECOUPLING`, `ERC-I2C-PULLUP`, `ERC-I2C-ADDRESS`, `ERC-USB-ESD`, `ERC-USB-CC`, `ERC-FLOATING-INPUT`, `ERC-FLYBACK`, `ERC-LED-RESISTOR`, `ERC-CAP-VOLTAGE` |
| Power | `PWR-SOURCE`, `PWR-REG-CAPACITY`, `PWR-DROPOUT`, `PWR-REG-INPUT`, `PWR-THERMAL`, `PWR-CHARGE-RATE`, `PWR-BATTERY-LIFE`, `PWR-NO-DATA` |
| DFM / placement | `DFM-PASSIVE-SIZE`, `DFM-PAD-PITCH`, `DFM-DRILL`, `DFM-THT`, `DFM-BGA`, `DFM-NETCLASS`, `DFM-COURTYARD-OVERLAP`, `DFM-OUTSIDE-BOARD`, `DFM-EDGE-CLEARANCE`, `DFM-KEEPOUT`, `DFM-UNPLACED`, `DFM-DECOUPLING-DISTANCE`, `DFM-CONNECTOR-EDGE`, `DFM-MIXED-ASSEMBLY` |
| Library | `LIB-UNVERIFIED`, `LIB-FOOTPRINT`, `LIB-LOW-CONFIDENCE`, `LIB-NO-DATASHEET` |
| KiCad DRC | `DRC-<type>` from `kicad-cli pcb drc`. Unrouted connections and silkscreen findings are expected on an unrouted, auto-placed board, so they are reported as info. Errors that lie entirely inside one library footprint (for example a module's 0.2 mm thermal vias) are reported as warnings. |

Status is `PASS`, `PASS_WITH_WARNINGS` or `FAIL` (any error).

### KiCad DRC

DRC runs during export when `kicad-cli` is available, either from
`PCBASSIST_KICAD_CLI` or from `kicad-cli` on `PATH`. Without KiCad installed:

```bash
export PCBASSIST_KICAD_CLI=$PWD/scripts/kicad-cli-docker   # runs kicad/kicad:8.0.9 in Docker
make test-kicad   # real DRC, plus KiCad's netlist from our schematic compared with the plan's
```

The `Dockerfile` builds on the official KiCad 8 image, so `kicad-cli` is always there:

```bash
docker build -t pcbassist .
docker run --rm -v "$PWD/out:/work/out" pcbassist generate \
  -f /opt/pcbassist/examples/relay_motor_12v/prompt.txt --out out/relay --mock-llm
docker run --rm -p 8000:8000 -v "$PWD/sessions:/work/sessions" pcbassist serve --host 0.0.0.0
```

## Examples

Committed in `examples/<name>/` (prompt and full session output). Regenerate with
`make examples`.

| Example | What it shows | Status |
|---|---|---|
| `iot_env_sensor` | ESP32-C3, USB-C + LiPo charger, LDO, SHT40, battery sense; plus a **v2 refinement** (SHT40 → BME280, see `out/diff_v1_v2.txt`) | PASS_WITH_WARNINGS (thin battery-life margin, LDO dropout at low battery, VBUS at its 500 mA limit) |
| `ble_coin_tag` | nRF52832 module on a CR2032, LIS3DH wake-on-motion, Tag-Connect SWD | PASS_WITH_WARNINGS |
| `relay_motor_12v` | 12 V input with fuse, series diode and TVS; LM2596 buck; LDO; ESP32; DRV8871 H-bridge; relay with FET + flyback; screw terminals; 4 M3 holes | PASS_WITH_WARNINGS |
| `flawed_coin_wifi` | **Intentionally flawed:** Wi-Fi on a coin cell, 3.3 V LDO from 3 V, 5 V relay on 3.3 V, two identical I2C sensors, 15 x 15 mm board, an off-library part | **FAIL**: 12 errors (I2C address clash, supply range, dropout, regulator capacity, source capacity, battery life, board too small...) |

## Mock mode and honesty notes

* **Mock fixtures are hand-authored**, not recordings. No API key was available while
  this was built. They live in `fixtures/llm/<stage>/<example>.json`, are generated from
  `scripts/author_fixtures.py`, and are written in exactly the JSON shape the live model is
  asked for. Lookup order is an exact prompt hash, then keyword match, then `_default`. A
  non-exact match adds a `MOCK-NEAREST` warning, because the plan then describes the
  nearest example, not your prompt. `generate --record-fixtures DIR` saves live answers as
  new fixtures.
* The live Anthropic path is tested against a faked SDK client only.
* **Pin tables** come from the official KiCad symbol libraries. Each pin is cross-checked
  against a pad of the KiCad footprint at seed-build time (`scripts/build_seed.py`). No IC
  pin table was typed by hand. Pins are still the KiCad community's data: verify against
  the datasheet.
* Electrical limits, currents and specs in `data/parts_curated.toml` are curated values
  with a `data_confidence` level. Design rules of thumb (regulator capacitors, ESP32
  strapping pull-ups, DRV8871 ILIM) are in `pipeline/synth.py`, each with its reasoning
  and a "verify" note.
* Relay NO/NC contacts and barrel-jack polarity are not asserted. The report asks the
  engineer to confirm them.

## Configuration

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | Live mode (never logged; redacted in the audit trail) |
| `PCBASSIST_MODEL` | Model id (default `claude-sonnet-5-5`) |
| `PCBASSIST_LLM_FALLBACKS` | `off` disables server-side fallback models |
| `PCBASSIST_LLM_EFFORT` | Optional effort setting passed to the API |
| `PCBASSIST_KICAD_CLI` | Path to `kicad-cli` (or `scripts/kicad-cli-docker`) |
| `PCBASSIST_DATA_DIR`, `PCBASSIST_FIXTURES_DIR` | Override the bundled data / fixtures |

## Development

```bash
make check-all     # ruff, ruff format --check, mypy --strict (core modules), pytest + 80% coverage, library validate
make test-kicad    # the two tests that need real KiCad
make seed          # rebuild the component library from KiCad sources (network)
make fixtures      # regenerate mock fixtures
```

Layout: `schemas/` (Pydantic models + JSON Schema export), `llm/` (client, mock,
structured retry, audit), `library/` (SQLite library, footprint resolver), `kicad/`
(S-expression I/O, footprint geometry), `pipeline/` (stages, synthesizer, constraints,
placement, routing, engine), `checks/`, `export/`, `review/` (sessions, decisions,
refine, diff), `ui/` (FastAPI and a static page). Design decisions and the milestone log
are in [PLAN.md](PLAN.md).

## Known limitations

* No routing (only the ratsnest), single-sided placement and 0° rotations. Edge
  connectors still need rotating so the mating face points off the board.
* The schematic is one flat sheet of generated box symbols with net labels. It is
  correct by construction (KiCad's netlist from it matches the plan) but not a
  hand-drawn schematic.
* Subcircuits are synthesized only for the parts the rules cover: ESP32/nRF52 modules,
  MCP73831, fixed LDOs, LM2596-5.0, I2C sensors, USB-C, relays, DRV8871, LEDs, buttons,
  SWD/UART/Qwiic headers and input protection. Any other part is wired to power and
  ground; its remaining pins are left as explicit no-connects with a `SYNTH-INCOMPLETE`
  warning.
* The fab profiles are generic and conservative, not any specific fab's published rules.
* Mains, safety and RF certification are out of scope.
