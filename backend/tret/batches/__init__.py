"""Batches: many engine runs from one register, one run per target.

`core` is the generic orchestrator; `profiles/` holds what differs per kind of
batch (the disclosure collection is the first). `tret/services/collections.py`
is the compatibility surface the HTTP layer and the tests import.
"""
