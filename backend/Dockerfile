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
COPY backend/constraints.txt ./

# `-c constraints.txt` pins every version pip resolves here, direct and
# transitive, to the set the test suite and CI are green against. Without it the
# image resolves the loose `>=` floors in pyproject.toml to whatever is newest on
# PyPI at build time, so two builds of the same commit can ship different
# dependency trees and the running stack can drift from what the tests actually
# validated. CONTRIBUTING calls the unconstrained install "the mistake this
# exists to prevent"; that applies to an image build more than anywhere, because
# nothing re-runs the suite against the result. Constraints only narrow versions
# -- they never add a package -- so the dev-only pins in the file are simply
# unused by this `.[server]` install.
#
# The install is split in two so that editing source does not re-resolve and
# re-download the whole dependency tree. This layer's only inputs are
# pyproject.toml and constraints.txt, so it stays cached until a dependency
# actually changes. hatchling cannot build tret without its package directory,
# and the real source must not land in this layer or the caching would be
# pointless -- so a stub stands in just long enough to resolve and install
# everything tret depends on, and the stub's own install is then removed, leaving
# the third-party tree behind.
RUN mkdir -p tret && touch tret/__init__.py \
    && pip install --no-cache-dir ".[server]" -c constraints.txt \
    && pip uninstall -y tret \
    && rm -rf tret

# The source arrives only now, so every layer above survives a source edit.
COPY backend/tret ./tret
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./

# Adds tret itself and its `tret` console script, nothing else: the dependencies
# are installed and pinned above, and --no-deps stops pip re-resolving them. This
# is the only pip layer a source-only change has to rebuild.
RUN pip install --no-cache-dir --no-deps .

# Reference documentation, served read-only by GET /api/docs/{slug} so the
# methodology is readable in-product instead of only in the repo. Kept as the
# single copy of that prose — the API reads this directory, nothing transcribes
# it. tests/test_docs_api.py fails if this line is dropped.
COPY docs ./docs

EXPOSE 8000
# Single worker by design: the run event bus is in-process (see docs/architecture.md).
CMD ["uvicorn", "tret.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
