.PHONY: help demo backend frontend test lint

.DEFAULT_GOAL := help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*##"}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

demo: ## One-command demo: docker compose up
	docker compose up --build

backend: ## Run backend locally (needs local postgres + .env)
	cd backend && .venv/bin/uvicorn tret.main:app --reload --port 8000

frontend: ## Run frontend dev server
	cd frontend && npm run dev

test: ## Run the backend test suite
	cd backend && .venv/bin/pytest -q

lint: ## Lint the backend with ruff
	cd backend && .venv/bin/ruff check tret
