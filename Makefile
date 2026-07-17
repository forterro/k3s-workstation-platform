.PHONY: sync lint format test preflight bootstrap reset deps generate-ca

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

reset:
	uv run k3s-workstation-bootstrap reset

deps:
	@for chart in umbrella-charts/*/*; do \
		if [ -f "$$chart/Chart.yaml" ]; then echo "==> $$chart"; helm dependency update "$$chart"; fi; \
	done

generate-ca:
	bash scripts/generate-ca.sh
