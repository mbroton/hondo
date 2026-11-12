.DEFAULT_GOAL := help

.PHONY: help format lint typecheck api-types build check test

help: ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

format: ## Auto-format code
	npm run format

lint: ## Lint code
	npm run lint

typecheck: ## Type-check with TypeScript
	npm run typecheck

api-types: ## Check the API types match openapi.json
	npm run api-types:check

build: ## Build the production bundle
	npm run build

check: lint typecheck api-types build ## Fast checks: lint + typecheck + API types + build

test: ## Run browser regression tests
	npm test
