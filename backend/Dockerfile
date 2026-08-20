# Build context is the REPOSITORY ROOT, not `backend/`.
#
# It has to be: the API serves `docs/emissions-methodology.md` to the product's
# methodology dialog (tret/api/docs.py), and `docs/` is a sibling of `backend/`,
# so a `backend/`-scoped context could not reach it. Copying the doc into the
# Python package instead would create a second copy to keep in step, which is the
# one thing the methodology document must never have.
#
# Build it with:
#   docker build -f backend/Dockerfile .
# docker-compose.yml says the same thing as `context: .` + `dockerfile:`.
# The root .dockerignore keeps the wider context small.
FROM python:3.12-slim

# WeasyPrint (PDF export) native dependencies + fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
    libcairo2 libffi8 shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/tret ./tret
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./

RUN pip install --no-cache-dir .

# Reference documentation, served read-only by GET /api/docs/{slug} so the
# methodology is readable in-product instead of only in the repo. Kept as the
# single copy of that prose — the API reads this directory, nothing transcribes
# it. tests/test_docs_api.py fails if this line is dropped.
COPY docs ./docs

EXPOSE 8000
# Single worker by design: the run event bus is in-process (see docs/architecture.md).
CMD ["uvicorn", "tret.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
