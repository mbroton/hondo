.DEFAULT_GOAL := help

.PHONY: help format lint typecheck api test-fast test check

help: ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

format: ## Auto-format code
	uv run ruff format src tests

lint: ## Lint and check formatting
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck: ## Type-check with pyright
	uv run pyright

api: ## Run the control-plane API
	uv run hondo api

test-fast: ## Run tests except process-level recovery
	uv run pytest -q -m "not slow"

test: ## Run the full test suite
	uv run pytest -q

check: lint typecheck ## Fast checks: lint + format + typecheck
