FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY bench ./bench
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install --no-cache-dir .

EXPOSE 8000
# Single worker by design: the run event bus is in-process (see docs/architecture.md).
CMD ["uvicorn", "bench.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
