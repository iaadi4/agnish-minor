.PHONY: install lint format typecheck test test-kicad cov seed seed-index fixtures check-all \
        kicad-libs kicad-index \
        demo serve-demo examples docker-build docker-demo

EXAMPLES := iot_env_sensor ble_coin_tag relay_motor_12v flawed_coin_wifi motor_current_monitor
REFINE := Replace the SHT40 with a BME280 so we also get barometric pressure.
# Use KiCad from Docker when it is not installed locally (override: make demo KICAD_CLI=kicad-cli)
KICAD_CLI ?= $(shell command -v kicad-cli 2>/dev/null || echo $(CURDIR)/scripts/kicad-cli-docker)

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check . --fix

typecheck:
	uv run mypy

test:
	uv run pytest

# Tests that drive the real kicad-cli (DRC + schematic netlist parity).
test-kicad:
	PCBASSIST_KICAD_CLI=$(KICAD_CLI) uv run pytest tests/test_export.py -k real -v

cov:
	uv run pytest --cov=pcbassist --cov-report=term-missing --cov-fail-under=80

# Rebuild data/components_seed.json from data/parts_curated.toml + KiCad libraries (network).
seed:
	uv run python scripts/build_seed.py

seed-index:
	uv run python scripts/build_seed.py --index

# Regenerate the mock fixtures (and their prompt hashes): hand-authored requirements /
# architecture / selection, then the parts + netlist fixtures derived from them.
fixtures:
	uv run python scripts/author_fixtures.py
	cd scripts && uv run python derive_intent_fixtures.py

# Copy the KiCad 8.0.9 symbol/footprint libraries out of the official Docker image into
# ~/.cache/pcbassist/kicad/8.0.9 (full footprint files for export), then rebuild the index.
kicad-libs:
	scripts/fetch-kicad-libs

kicad-index:
	uv run python scripts/build_kicad_index.py

check-all: lint typecheck cov
	uv run pcbassist library validate

# End-to-end demo in mock mode: four designs + one refinement, KiCad DRC if available.
demo:
	rm -rf out/demo
	@for ex in $(EXAMPLES); do \
	  echo "=== $$ex"; \
	  PCBASSIST_KICAD_CLI=$(KICAD_CLI) uv run pcbassist generate -f examples/$$ex/prompt.txt \
	    --out out/demo/$$ex --mock-llm || exit 1; \
	done
	PCBASSIST_KICAD_CLI=$(KICAD_CLI) uv run pcbassist refine out/demo/iot_env_sensor --mock-llm \
	  -f "$(REFINE)"
	@echo; echo "Reports: out/demo/*/v1/report.md   Review UI: make serve-demo"

serve-demo:
	uv run pcbassist serve --sessions out/demo

# Refresh the committed example outputs in examples/<name>/out.
examples:
	@for ex in $(EXAMPLES); do \
	  rm -rf examples/$$ex/out; \
	  PCBASSIST_KICAD_CLI=$(KICAD_CLI) uv run pcbassist generate -f examples/$$ex/prompt.txt \
	    --out examples/$$ex/out --mock-llm || exit 1; \
	done
	PCBASSIST_KICAD_CLI=$(KICAD_CLI) uv run pcbassist refine examples/iot_env_sensor/out \
	  --mock-llm -f "$(REFINE)" > examples/iot_env_sensor/out/refine_v2.txt
	uv run pcbassist diff examples/iot_env_sensor/out/plan.v1.json \
	  examples/iot_env_sensor/out/plan.v2.json > examples/iot_env_sensor/out/diff_v1_v2.txt

docker-build:
	docker build -t pcbassist .

docker-demo: docker-build
	mkdir -p out
	docker run --rm -v "$(CURDIR)/out:/work/out" pcbassist generate \
	  -f /opt/pcbassist/examples/iot_env_sensor/prompt.txt --out out/docker-iot --mock-llm
