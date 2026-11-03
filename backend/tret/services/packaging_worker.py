"""Child-process entry point for `packaging.run_packaging`.

Run as a script (`python -I packaging_worker.py`), never imported by the API. It
reads one JSON job on stdin and writes one JSON result on stdout.

Why a separate process, and why it loads `packaging.py` by path instead of
importing `tret.services.packaging`:

* pypdf decodes streams into memory with no total cap, and the source is a file a
  stranger published. A thread cannot be stopped or bounded, and an out-of-memory
  parse would take the API process with it. A child can be given an address-space
  and CPU ceiling and be killed at a deadline.
* The limits must be in force before pypdf is imported, and importing the `tret`
  package pulls in the whole SDK (1500 modules) first. Loading the one file by
  path keeps the child small enough for a tight ceiling and quick to start.

stdout carries only the result: the real stdout is saved and fd 1 is pointed at
stderr, so a stray print from a library cannot corrupt it.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any


def _apply_limits(job: dict[str, Any]) -> None:
    try:
        import resource
    except ImportError:  # not POSIX; the parent's deadline still applies
        return
    limits = (
        ("RLIMIT_AS", int(job["max_rss_bytes"])),
        ("RLIMIT_CPU", int(job["deadline_seconds"]) + 30),
    )
    for name, value in limits:
        try:
            resource.setrlimit(getattr(resource, name), (value, value))
        except (AttributeError, ValueError, OSError) as exc:
            # macOS refuses RLIMIT_AS; the parent still enforces the deadline.
            print(f"packaging worker: could not set {name}: {exc}", file=sys.stderr)


def _load_packaging() -> Any:
    path = Path(__file__).with_name("packaging.py")
    spec = importlib.util.spec_from_file_location("_tret_packaging_child", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(job: dict[str, Any]) -> dict[str, Any]:
    pk = _load_packaging()
    try:
        m = job["mapping"]
        mapping = pk.PackageMap(
            title=m["title"],
            role=m["role"],
            scope=m["scope"],
            keep_ranges=None if m["keep_ranges"] is None else tuple(tuple(r) for r in m["keep_ranges"]),
            review_note=m.get("review_note", ""),
            incorporations=tuple(m.get("incorporations") or ()),
            exclusions=tuple(m.get("exclusions") or ()),
        )
        output = pk.package_pdf(
            Path(job["source_path"]),
            mapping,
            Path(job["out_dir"]),
            stem=job["stem"],
            source_meta=job["source_meta"],
            deadline_seconds=float(job["deadline_seconds"]),
            _stage_dir=Path(job["stage_dir"]),
        )
    except pk.PackageMapError as exc:
        return {"ok": False, "kind": "map", "message": str(exc)}
    except pk.PackagingError as exc:
        return {"ok": False, "kind": "packaging", "message": str(exc)}
    except MemoryError:
        return {"ok": False, "kind": "packaging", "message": "packaging ran out of memory"}
    except Exception as exc:  # a hostile PDF can raise anything; the parent gets one error type
        return {"ok": False, "kind": "packaging", "message": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "output": dataclasses.asdict(output)}


def main() -> int:
    job = json.loads(sys.stdin.buffer.read())
    result_stream = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    _apply_limits(job)
    result = _run(job)
    result_stream.write(json.dumps(result, default=str).encode("utf-8"))
    result_stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
