.PHONY: sync lint format test preflight bootstrap

sync:
	uv sync

lint:
	uv run ruff check src

format:
	uv run ruff format src

test:
	uv run pytest

preflight:
	uv run k3s-workstation-bootstrap preflight

bootstrap:
	uv run k3s-workstation-bootstrap bootstrap
