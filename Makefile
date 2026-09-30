.PHONY: check check-all lint type test test-corpus fmt
check: lint type test
check-all: check test-corpus
lint:
	uv run ruff check src tests scripts
fmt:
	uv run ruff format src tests scripts
type:
	uv run mypy
test:
	uv run pytest
test-corpus:
	uv run pytest -m corpus
