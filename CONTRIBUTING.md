# Contributing to bench

Thanks for your interest! bench is early — the most valuable contributions
right now are **domain packs**, provider integrations, and hardening.

## Development setup

```bash
# Postgres
docker run -d --name bench-pg -e POSTGRES_USER=bench -e POSTGRES_PASSWORD=bench \
  -e POSTGRES_DB=bench -p 5432:5432 postgres:16-alpine

# Backend
cd backend
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
BENCH_PACKS_DIR=../packs .venv/bin/uvicorn bench.main:app --reload

# Frontend (proxies /api to :8000)
cd frontend && npm install && npm run dev
```

## Before you open a PR

```bash
cd backend && .venv/bin/ruff check bench && .venv/bin/pytest -q
cd frontend && npm run build
```

## Ground rules

- **The trust doctrine is not negotiable** (docs/trust-doctrine.md). PRs that
  let the model bypass dataset-only numbers, self-approve findings, or skip
  provenance will be declined regardless of how convenient they are.
- New providers implement `bench/providers/base.py` and add curated entries
  to `models.yaml` with honest prices and strengths.
- New packs must pass `bench packs validate` and ship enough fictional
  sample data to demo every task type. No real client data, ever.
- Keep the single-worker event-bus constraint in mind (docs/architecture.md)
  until the LISTEN/NOTIFY bus lands.
