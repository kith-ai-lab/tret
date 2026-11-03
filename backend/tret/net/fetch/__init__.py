"""URL -> checked fetch -> snapshot -> Document. The research evidence path."""
from tret.net.fetch.extract import html_to_text
from tret.net.fetch.fetch import (
    CAPTURE_TYPES,
    MAX_REDIRECTS,
    READABLE_TYPES,
    FetchedPage,
    FetchError,
    fetch_page,
)
from tret.net.fetch.snapshot import (
    PROVENANCE_WEB,
    PROVENANCE_WEB_CAPTURE,
    SOURCE_KIND_UPLOAD,
    SOURCE_KIND_WEB,
    find_snapshot,
    snapshot_filename,
    store_snapshot,
)

__all__ = [
    "CAPTURE_TYPES",
    "MAX_REDIRECTS",
    "PROVENANCE_WEB",
    "PROVENANCE_WEB_CAPTURE",
    "READABLE_TYPES",
    "SOURCE_KIND_UPLOAD",
    "SOURCE_KIND_WEB",
    "FetchError",
    "FetchedPage",
    "fetch_page",
    "find_snapshot",
    "html_to_text",
    "snapshot_filename",
    "store_snapshot",
]
