from tret.sdk import Receipt, Router, RunResult

# The version reported in `FastAPI(version=...)` (tret/main.py) and in every
# telemetry payload's `tret_version` field (tret/services/telemetry.py). One
# constant so the two can never drift — pyproject.toml's own version stays a
# separate, manually-bumped release number, not read from here.
__version__ = "0.1.0"

__all__ = ["Receipt", "Router", "RunResult", "__version__"]
