FROM python:3.12-slim

# WeasyPrint (PDF export) native dependencies + fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
    libcairo2 libffi8 shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY bench ./bench
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install --no-cache-dir .

EXPOSE 8000
# Single worker by design: the run event bus is in-process (see docs/architecture.md).
CMD ["uvicorn", "bench.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
