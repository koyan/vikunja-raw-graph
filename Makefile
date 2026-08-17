SHELL := /bin/bash
include .env
export

.PHONY: help up down restart stop start logs logs-api ps status pull update backup restore-help shell-db shell-api reset-password env-check \
	raw-graph raw-graph-build raw-graph-clean raw-graph-server-logs

RAW_GRAPH_DIR := raw-graph-export
RAW_GRAPH_DATA := $(RAW_GRAPH_DIR)/data

# Default target: show help
help:
	@echo "Vikunja stack — available commands:"
	@echo "  make up            Start the stack in the background"
	@echo "  make down          Stop and remove containers (volumes are kept)"
	@echo "  make stop          Stop containers without removing them"
	@echo "  make start         Start previously stopped containers"
	@echo "  make restart       Restart all containers"
	@echo "  make logs          Tail logs for all services"
	@echo "  make logs-api      Tail logs for the Vikunja API service only"
	@echo "  make ps            Show status of running containers"
	@echo "  make pull          Pull the latest images"
	@echo "  make update        Pull latest images and recreate containers"
	@echo "  make backup        Back up the Postgres database to ./backups"
	@echo "  make shell-db      Open a psql shell inside the db container"
	@echo "  make shell-api     Open a shell inside the api container"
	@echo "  make env-check     Verify .env exists before starting"
	@echo ""
	@echo "  make raw-graph             Export tasks and render a real GraphViz dependency graph (SVG)"
	@echo "  make raw-graph-build       Build/update the raw-graph images"
	@echo "  make raw-graph-clean       Remove raw-graph's generated output"
	@echo "  make raw-graph-server-logs Tail logs for the always-on /raw-graph HTTP endpoint"
	@echo ""
	@echo "  The /raw-graph HTTP endpoint (raw-graph-server) starts with 'make up' like"
	@echo "  the rest of the stack -- reachable via Caddy at https://<DOMAIN>/raw-graph,"
	@echo "  gated by RAW_GRAPH_USERNAME/PASSWORD in .env."

env-check:
	@if [ ! -f .env ]; then \
		echo "ERROR: .env file not found. Copy .env.example to .env and fill in your values first."; \
		exit 1; \
	fi

up: env-check
	docker compose up -d
	@echo "Vikunja is starting. Check 'make logs' if it doesn't come up within a minute."

down:
	docker compose down

stop:
	docker compose stop

start:
	docker compose start

restart:
	docker compose restart

logs:
	docker compose logs -f

logs-api:
	docker compose logs -f api

ps:
	docker compose ps

pull:
	docker compose pull

update: pull
	docker compose up -d
	@echo "Stack updated to latest images."

# Dumps the Postgres database to a timestamped .sql file in ./backups
backup:
	@mkdir -p backups
	@set -a; . ./.env; set +a; \
	FILENAME="backups/vikunja-$$(date +%Y-%m-%d_%H-%M-%S).sql"; \
	docker compose exec -T db pg_dump -U $$POSTGRES_USER $$POSTGRES_DB > $$FILENAME; \
	echo "Backup written to $$FILENAME"

shell-db:
	@set -a; . ./.env; set +a; \
	docker compose exec db psql -U $$POSTGRES_USER -d $$POSTGRES_DB

shell-api:
	docker compose exec api sh

# --- Raw dependency graph via GraphViz dot (see raw-graph-export/) ------
# Uses Hochfrequenz's taskdependencygraph + a self-hosted kroki to render a
# real hierarchical layout (rankdir=LR) -- unlike both depviz views, node
# position here is actually determined by dependency structure, not a grid
# or per-node rank. kroki/mermaid are always-on (started by `make up`) so
# both this one-off CLI run and the always-on /raw-graph HTTP endpoint
# (raw-graph-server, also started by `make up`) can use them; `docker
# compose run` starts them automatically via depends_on if they aren't
# already up.
raw-graph: env-check
	@mkdir -p $(RAW_GRAPH_DATA)
	docker compose run --rm --user "$$(id -u):$$(id -g)" raw-graph-export
	@echo "Graph written to $(RAW_GRAPH_DATA)/raw_graph.svg"

raw-graph-build:
	docker compose build raw-graph-export raw-graph-server

raw-graph-clean:
	rm -rf $(RAW_GRAPH_DATA)

raw-graph-server-logs:
	docker compose logs -f raw-graph-server

ssh:
	ssh -t ${SERVER_USER}@${SERVER_IP} "cd ${SERVER_DIRECTORY} ; bash --login"

# Fetches the most recent backup from the server to your local ./backups folder
dbfetch:
	@mkdir -p backups
	@LATEST=$$(ssh ${SERVER_USER}@${SERVER_IP} "ls -t ${SERVER_DIRECTORY}/backups/*.sql 2>/dev/null | head -n1"); \
	if [ -z "$$LATEST" ]; then \
		echo "No backup files found on server. Run 'make backup' there first."; \
		exit 1; \
	fi; \
	echo "Fetching $$LATEST ..."; \
	scp ${SERVER_USER}@${SERVER_IP}:"$$LATEST" backups/; \
	echo "Saved to backups/$$(basename $$LATEST)"

# Imports the most recent local backup into your local running Postgres container
# WARNING: this wipes existing local data before restoring
dbimport:
	@LATEST=$$(ls -t backups/*.sql 2>/dev/null | head -n1); \
	if [ -z "$$LATEST" ]; then \
		echo "No local backup files found in ./backups. Run 'make dbfetch' first."; \
		exit 1; \
	fi; \
	read -p "This will WIPE your local database and restore $$LATEST. Continue? [y/N] " confirm; \
	if [ "$$confirm" != "y" ]; then \
		echo "Aborted."; \
		exit 1; \
	fi; \
	set -a; . ./.env; set +a; \
	echo "Wiping local schema..."; \
	docker compose exec -T db psql -U $$POSTGRES_USER -d $$POSTGRES_DB -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"; \
	echo "Importing $$LATEST ..."; \
	cat "$$LATEST" | docker compose exec -T db psql -U $$POSTGRES_USER -d $$POSTGRES_DB; \
	echo "Import complete."