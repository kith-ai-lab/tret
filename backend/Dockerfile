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
# poppler-utils: pdftotext gives page-referenced layout text for disclosure packaging
# (tret/services/packaging.py); the pypdf fallback works but is lower fidelity.
# poppler-data: CJK and other non-Latin glyph maps for pdftotext; Recommends, so
# --no-install-recommends would skip it and that text would extract as nothing.
# tesseract-ocr(+eng): OCR supplement for scanned disclosure pages; English only by design.
# bubblewrap: the security boundary of the Learning edition's unvetted analysis lane
# (tret/services/analysis.py): model-written Python runs inside a bwrap sandbox with no
# host filesystem, network or /proc. The lane refuses to run without it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
    libcairo2 libffi8 shared-mime-info fonts-dejavu-core poppler-utils poppler-data \
    tesseract-ocr tesseract-ocr-eng bubblewrap \
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
    && pip install --no-cache-dir ".[server,s3]" -c constraints.txt \
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

# Non-root runtime identity, same as Dockerfile.fly (docs/hardening.md:
# "container/VM with a non-root user"). Fixed uid/gid 10001 so volume ownership
# is stable across rebuilds and matches the Fly image. HOME=/app for the same
# reason as there: asyncpg stats $HOME/.postgresql/ on every connection, and
# under /root that is a PermissionError for this user, not a miss.
RUN groupadd --gid 10001 tret \
    && useradd --uid 10001 --gid tret --home /app --no-create-home --shell /usr/sbin/nologin tret \
    && chown -R tret:tret /app
ENV HOME=/app

# Starts as root only to chown the named `storage` volume (created root-owned,
# and root-written by installs that predate this user), then setpriv drops to
# `tret` and execs uvicorn. A Dockerfile `USER` line would defeat that. /packs is
# a read-only bind mount in compose, so nothing there needs a chown.
COPY fly-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
# Single worker by design: the run event bus is in-process (see docs/architecture.md).
CMD ["uvicorn", "tret.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
