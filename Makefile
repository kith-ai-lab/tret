.PHONY: dev demo backend frontend test lint

demo: ## One-command demo: docker compose up
	docker compose up --build

backend: ## Run backend locally (needs local postgres + .env)
	cd backend && .venv/bin/uvicorn bench.main:app --reload --port 8000

frontend: ## Run frontend dev server
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/pytest -q

lint:
	cd backend && .venv/bin/ruff check bench
