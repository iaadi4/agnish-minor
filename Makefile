.PHONY: install lint format typecheck test cov seed seed-index check-all

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

cov:
	uv run pytest --cov=pcbassist --cov-report=term-missing --cov-fail-under=80

# Rebuild data/components_seed.json from data/parts_curated.toml + KiCad libraries (network).
seed:
	uv run python scripts/build_seed.py

seed-index:
	uv run python scripts/build_seed.py --index

check-all: lint typecheck cov
	uv run pcbassist library validate
