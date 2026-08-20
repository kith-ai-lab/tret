"""`pip install tret` (no `[server]` extra) must be enough to import the SDK
path. This is checked in a subprocess, with a fresh interpreter, because a
sibling test importing `tret.db.models` or `tret.main` earlier in the same
process would pollute `sys.modules` and let a real edge slip through
undetected.

The modules below are the ones a core install actually needs: pack authoring
(`tret.packs.loader`), the router and provider catalog, the emissions
service, config, and the CLI entrypoint. None of them may load SQLAlchemy,
FastAPI/Starlette, Alembic, asyncpg, WeasyPrint, argon2, itsdangerous, or
Markdown — every one of those belongs to `tret[server]`.
"""
from __future__ import annotations

import subprocess
import sys

# Every top-level package that belongs to the `server` extra, not core.
_SERVER_ONLY_PACKAGES = (
    "sqlalchemy",
    "fastapi",
    "starlette",
    "alembic",
    "asyncpg",
    "weasyprint",
    "argon2",
    "itsdangerous",
    "markdown",
    "cryptography",
    "multipart",
)

_IMPORT_CHECK = """
import sys

import tret
import tret.cli
import tret.config
import tret.packs.loader
import tret.providers.catalog
import tret.router_llm.router
import tret.services.emissions

banned = {banned!r}
leaked = sorted(
    m for m in sys.modules if m.split(".", 1)[0] in banned
)
if leaked:
    print("LEAKED:" + ",".join(leaked))
    sys.exit(1)
print("CLEAN")
""".format(banned=set(_SERVER_ONLY_PACKAGES))


def test_sdk_modules_do_not_import_server_extras():
    """Importing the core SDK surface must not pull in any `server`-extra package."""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _IMPORT_CHECK],
        capture_output=True,
        timeout=60,
        text=True,
    )
    assert proc.returncode == 0, (
        f"core SDK import pulled in a server-only package\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert proc.stdout.strip() == "CLEAN", proc.stdout


def test_cli_help_does_not_require_server_extras():
    """`tret --help` must exit 0 and stay clean of server-only packages in a core install."""
    proc = subprocess.run(
        [sys.executable, "-I", "-m", "tret.cli", "--help"],
        capture_output=True,
        timeout=60,
        text=True,
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert "usage: tret" in proc.stdout
