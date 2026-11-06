"""Compatibility alias: the batch API moved to `tret.api.batches`, which serves
both `/api/batches` and the deprecated `/api/collections`."""
from tret.api.batches import router  # noqa: F401
