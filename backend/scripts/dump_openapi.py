"""Write tret's OpenAPI document to a file (or stdout), without a database.

    python backend/scripts/dump_openapi.py sdk/typescript/openapi.json

The TypeScript SDK's generated types (sdk/typescript/src/generated/openapi.ts)
are built from the committed snapshot this writes; CI regenerates it and fails
on any diff, so a backend change that moves the API surface also has to move
the snapshot (and the SDK) in the same commit.

No database, network or secrets are needed: importing `tret.main` only builds
the FastAPI app — the database is first touched in its lifespan, which this
never enters. What *does* change the schema is configuration, because some
routers are mounted conditionally (the OIDC router when `TRET_OIDC_ISSUER` is
set, an extension's routers when `TRET_EXTENSIONS` names one, the SPA catch-all
when `TRET_SERVE_FRONTEND_DIR` is set — the last is `include_in_schema=False`
anyway). So the dump is taken against the stock configuration on purpose:
every `TRET_*` variable is removed from the environment and the working
directory is moved to an empty temp dir so pydantic-settings' `.env` lookup
(relative to the cwd) finds nothing. The result describes the open-source core
API, the same on every machine.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def build_schema() -> dict:
    saved_env = dict(os.environ)
    for key in [k for k in os.environ if k.startswith("TRET_")]:
        del os.environ[key]
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from tret.config import get_settings  # reads no settings at import

    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as empty:
        os.chdir(empty)
        try:
            # After the chdir: importing tret.main builds the module-level app,
            # which reads settings (and a `.env` in the cwd, were there one).
            from tret.main import create_app

            # Settings are cached per process; drop anything read under the
            # caller's configuration before building the stock app.
            get_settings.cache_clear()
            return create_app().openapi()
        finally:
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(saved_env)
            get_settings.cache_clear()


def main(argv: list[str]) -> int:
    schema = build_schema()
    text = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if len(argv) > 1 and argv[1] != "-":
        Path(argv[1]).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
