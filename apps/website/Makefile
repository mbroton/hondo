.DEFAULT_GOAL := help

.PHONY: help dev format check build

help: ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

dev: ## Run the website development server
	npm run dev

format: ## Auto-format code
	npm run format

build: ## Build the static site into dist/
	npm run build

check: ## Fast checks: format + Astro type check + build
	npm run format:check
	npm run check
	npm run build
