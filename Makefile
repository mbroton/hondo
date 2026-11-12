.DEFAULT_GOAL := help

DEV_DB := docker compose -p hondo-dev --profile dev
AGENT_BOT := HONDO_ENV_FILE=$(HOME)/.config/hondo/agent.env HONDO_PORT=127.0.0.1:5175 HONDO_IMAGE=hondo-agent:local docker compose -p hondo-agent

.PHONY: help api worker client website db-up db-down agent-bot-up agent-bot-logs agent-bot-down api-types check test

help: ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

api: ## Run the API
	cd apps/server && uv run --env-file ../../.env hondo api

worker: ## Run the Slack worker
	cd apps/server && uv run --env-file ../../.env hondo serve

client: ## Run the client development server
	cd apps/client && npm run dev

website: ## Run the website development server
	cd apps/website && npm run dev

db-up: ## Start Postgres in the background and wait until ready
	$(DEV_DB) up -d --wait dev-postgres

db-down: ## Stop Postgres; keep database data
	$(DEV_DB) down

agent-bot-up: ## Build this tree, start the agent bot, wait until it is in Slack
	$(AGENT_BOT) up -d --build --force-recreate --remove-orphans --wait
	@for i in $$(seq 60); do \
		if $(AGENT_BOT) logs worker | grep -q "Bolt app is running"; then echo "agent bot is up"; exit 0; fi; \
		if [ -n "$$($(AGENT_BOT) ps -q --status exited --status restarting worker)" ]; then $(AGENT_BOT) logs --tail 20 worker; exit 1; fi; \
		sleep 1; \
	done; echo "agent bot did not connect to Slack in 60 s"; exit 1

agent-bot-logs: ## Show recent agent bot logs
	$(AGENT_BOT) logs --tail 200 worker api

agent-bot-down: ## Stop the agent bot; keep its database
	$(AGENT_BOT) down

api-types: ## Regenerate the client API types from the server schema
	cd apps/server && uv run python -m hondo.api.openapi > ../client/src/lib/api/openapi.json
	cd apps/client && npm run api-types

check: ## Fast checks for every app (server + client + website)
	$(MAKE) -C apps/server check
	$(MAKE) -C apps/client check
	$(MAKE) -C apps/website check

test: ## Server and client test suites
	$(MAKE) -C apps/server test
	$(MAKE) -C apps/client test
