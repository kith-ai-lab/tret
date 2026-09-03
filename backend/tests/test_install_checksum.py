"""install.sh.sha256 must never drift from install.sh.

install.sh is delivered as `curl | bash` from tret.kithailab.com, a host the
verification README.md recommends (its "verify before running" block) has no
reason to trust itself. install.sh.sha256, fetched straight from GitHub's raw
content host instead, is what lets someone check the piped script against
something other than the site serving it. That only works if the checksum
file is never allowed to go stale — this test recomputes install.sh's digest
on every run and fails the build the moment a change to install.sh isn't
matched by a regenerated checksum.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO / "install.sh"
CHECKSUM_FILE = REPO / "install.sh.sha256"

# The plain `shasum -a 256` output format: a lowercase hex digest, two spaces
# (binary mode), then the filename it was computed against.
CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  (\S+)\n?$")


def test_checksum_file_exists():
    assert CHECKSUM_FILE.exists(), (
        "install.sh.sha256 is missing from the repo root — README.md's "
        "verify-before-running instructions depend on it"
    )


def test_checksum_file_is_well_formed():
    line = CHECKSUM_FILE.read_text()
    match = CHECKSUM_LINE.match(line)
    assert match, (
        f"install.sh.sha256 must be exactly `shasum -a 256`'s own output "
        f"format (one line, <64 hex chars><two spaces><filename>), got {line!r}"
    )


def test_checksum_matches_install_sh_exactly():
    """The whole point: regenerate with `shasum -a 256 install.sh > "
    "install.sh.sha256` from the repo root whenever install.sh changes."""
    match = CHECKSUM_LINE.match(CHECKSUM_FILE.read_text())
    assert match, "install.sh.sha256 is malformed — see test_checksum_file_is_well_formed"
    recorded_digest, recorded_name = match.groups()

    assert recorded_name == "install.sh", (
        f"install.sh.sha256 names {recorded_name!r}, not install.sh — "
        "`shasum -c` verifies the file at this relative path"
    )

    actual_digest = hashlib.sha256(INSTALL_SH.read_bytes()).hexdigest()
    assert actual_digest == recorded_digest, (
        "install.sh.sha256 is stale: it does not match install.sh's current contents. "
        "Regenerate it with `shasum -a 256 install.sh > install.sh.sha256` from the repo root."
    )
