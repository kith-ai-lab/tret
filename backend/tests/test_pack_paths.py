"""Manifest path confinement (`tret.packs.paths`).

Every path `pack.yaml` names — doctrine, a task's output_schema, a dataset's
file, a method's entrypoint — is author-controlled, and an archive upload or
a builder draft lets any workspace admin author one. Before this check,
`doctrine: ["/proc/self/environ"]` installed cleanly and `GET /api/packs/{id}`
served the server's environment back. These tests pin the refusal at both
ends: `validate_pack` never installs such a pack, and every runtime reader
(the doctrine viewer, doctrine blocks, the method runner) refuses one that a
pre-fix install already stored.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tret.engine.context import doctrine_blocks, doctrine_sha
from tret.packs.loader import validate_pack
from tret.packs.paths import (
    PackFileMissing,
    PackPathError,
    pack_relpath_problem,
    resolve_pack_file,
)
from tret.services.methods import MethodError, execute_method

SECRET = "TRET_SECRET_KEY=not-a-real-secret"


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """A pack directory with one real doctrine file, and a secret file
    beside (outside) it."""
    pack = tmp_path / "pack"
    (pack / "doctrine").mkdir(parents=True)
    (pack / "doctrine" / "ok.md").write_text("# OK\n\nInside the pack.\n")
    secret = tmp_path / "outside-secret.txt"
    secret.write_text(SECRET)
    return pack, secret


# ── resolve_pack_file ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        "/etc/passwd",
        "/proc/self/environ",
        "/dev/zero",
        "../outside-secret.txt",
        "doctrine/../../outside-secret.txt",
        "..",
        "C:/Windows/win.ini",
        "doctrine\\ok.md",
        "doctrine/ok.md\x00",
        "",
        "   ",
    ],
)
def test_lexically_invalid_paths_are_refused(layout, rel):
    pack, _ = layout
    assert pack_relpath_problem(rel) is not None
    with pytest.raises(PackPathError) as info:
        resolve_pack_file(pack, rel)
    assert not isinstance(info.value, PackFileMissing)


def test_non_string_paths_are_refused(layout):
    pack, _ = layout
    for rel in (None, 3, ["doctrine/ok.md"]):
        with pytest.raises(PackPathError):
            resolve_pack_file(pack, rel)


def test_a_plain_relative_file_resolves_inside_the_pack(layout):
    pack, _ = layout
    resolved = resolve_pack_file(pack, "doctrine/ok.md")
    assert resolved == (pack / "doctrine" / "ok.md").resolve()
    assert "Inside the pack" in resolved.read_text()


def test_a_symlink_out_of_the_pack_is_refused(layout):
    pack, secret = layout
    (pack / "doctrine" / "link.md").symlink_to(secret)
    with pytest.raises(PackPathError, match="outside the pack"):
        resolve_pack_file(pack, "doctrine/link.md")


def test_a_symlinked_directory_out_of_the_pack_is_refused(layout, tmp_path):
    pack, _ = layout
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.md").write_text("outside")
    (pack / "linked").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(PackPathError, match="outside the pack"):
        resolve_pack_file(pack, "linked/x.md")


def test_a_symlink_that_stays_inside_the_pack_is_allowed(layout):
    pack, _ = layout
    (pack / "alias.md").symlink_to(pack / "doctrine" / "ok.md")
    assert resolve_pack_file(pack, "alias.md") == (pack / "doctrine" / "ok.md").resolve()


def test_a_directory_is_not_a_pack_file(layout):
    pack, _ = layout
    with pytest.raises(PackPathError, match="not a regular file"):
        resolve_pack_file(pack, "doctrine")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_is_refused_without_being_opened(layout):
    """Opening a FIFO for reading blocks until a writer appears, which is the
    `/dev/zero`-style hang this check exists to prevent."""
    pack, _ = layout
    os.mkfifo(pack / "doctrine" / "pipe.md")
    with pytest.raises(PackPathError, match="not a regular file"):
        resolve_pack_file(pack, "doctrine/pipe.md")


def test_a_missing_file_is_reported_as_missing(layout):
    pack, _ = layout
    with pytest.raises(PackFileMissing):
        resolve_pack_file(pack, "doctrine/absent.md")


def test_pack_path_error_is_an_oserror():
    """Runtime readers rely on their existing `except OSError` branch."""
    assert issubclass(PackPathError, OSError)


# ── validate_pack ────────────────────────────────────────────────────────────


def _write_manifest(pack: Path, body: str) -> None:
    (pack / "pack.yaml").write_text(
        "pack: sample\nversion: 0.1.0\ndisplay_name: Sample\n" + body
    )


@pytest.mark.parametrize("rel_kind", ["absolute", "dotdot", "symlink"])
def test_validate_pack_rejects_escaping_doctrine(layout, rel_kind):
    pack, secret = layout
    if rel_kind == "absolute":
        rel = str(secret)
    elif rel_kind == "dotdot":
        rel = "../outside-secret.txt"
    else:
        (pack / "doctrine" / "link.md").symlink_to(secret)
        rel = "doctrine/link.md"
    _write_manifest(pack, f"doctrine:\n  - doctrine/ok.md\n  - {rel!r}\n")

    _, _, errors = validate_pack(pack)

    assert any(e.startswith("doctrine file rejected:") for e in errors), errors
    assert not any(SECRET in e for e in errors)


def test_validate_pack_rejects_a_device_as_doctrine(layout):
    pack, _ = layout
    _write_manifest(pack, "doctrine:\n  - /dev/zero\n")
    _, _, errors = validate_pack(pack)
    assert any("doctrine file rejected" in e for e in errors), errors


def test_validate_pack_does_not_read_an_escaping_doctrine_for_a_task_selector(layout):
    """A task's `doctrine:` selector with a heading reads the file to check
    the heading exists. That read must go through the same confinement, so
    an escaping entry is reported once (by the doctrine-list check) and
    never opened."""
    pack, secret = layout
    _write_manifest(
        pack,
        f"doctrine:\n  - {str(secret)!r}\n"
        "task_types:\n"
        "  - slug: t\n    display_name: T\n    shape: freeform\n"
        f"    doctrine:\n      - {str(secret) + '#Heading'!r}\n",
    )
    _, _, errors = validate_pack(pack)
    assert any(e.startswith("doctrine file rejected:") for e in errors), errors
    assert not any("names a heading" in e for e in errors), errors


def test_validate_pack_rejects_an_escaping_output_schema(layout, tmp_path):
    pack, _ = layout
    outside_schema = tmp_path / "outside.schema.json"
    outside_schema.write_text('{"type": "object"}')
    _write_manifest(
        pack,
        "task_types:\n"
        "  - slug: t\n    display_name: T\n    shape: freeform\n"
        "    output_schema: ../outside.schema.json\n",
    )
    _, schemas, errors = validate_pack(pack)
    assert any(e.startswith("task 't': schema file rejected:") for e in errors), errors
    assert schemas == {}


def test_validate_pack_rejects_an_escaping_dataset(layout, tmp_path):
    pack, _ = layout
    (tmp_path / "outside.csv").write_text("a,b\n1,2\n")
    _write_manifest(pack, "datasets:\n  - name: d\n    file: ../outside.csv\n")
    _, _, errors = validate_pack(pack)
    assert any(e.startswith("dataset file rejected:") for e in errors), errors


def test_validate_pack_rejects_an_escaping_method_entrypoint(layout, tmp_path):
    pack, _ = layout
    (tmp_path / "outside.py").write_text("print('{}')\n")
    _write_manifest(
        pack,
        "methods:\n"
        "  - slug: m\n    display_name: M\n    entrypoint: ../outside.py\n",
    )
    _, _, errors = validate_pack(pack)
    assert any(e.startswith("method 'm': entrypoint rejected:") for e in errors), errors


def test_validate_pack_keeps_the_missing_file_wording(layout):
    pack, _ = layout
    _write_manifest(pack, "doctrine:\n  - doctrine/absent.md\n")
    _, _, errors = validate_pack(pack)
    assert "doctrine file missing: doctrine/absent.md" in errors


def test_validate_pack_accepts_a_contained_pack(layout):
    pack, _ = layout
    _write_manifest(pack, "doctrine:\n  - doctrine/ok.md\n")
    _, _, errors = validate_pack(pack)
    assert errors == []


# ── runtime readers: a pack row stored before the check existed ─────────────


def test_doctrine_blocks_skip_an_escaping_stored_path(layout):
    pack_dir, secret = layout
    stored = SimpleNamespace(
        source_path=str(pack_dir),
        manifest={"doctrine": ["doctrine/ok.md", str(secret), "../outside-secret.txt"]},
    )
    blocks = doctrine_blocks(stored, None)
    rendered = "\n".join(b.text for b in blocks)
    assert "Inside the pack" in rendered
    assert SECRET not in rendered


def test_doctrine_sha_refuses_an_escaping_path(layout):
    pack_dir, secret = layout
    with pytest.raises(PackPathError):
        doctrine_sha(pack_dir, [str(secret)])


async def test_execute_method_refuses_an_escaping_stored_entrypoint(layout, tmp_path):
    """Refused before any database work, so no session is needed."""
    pack_dir, _ = layout
    (tmp_path / "outside.py").write_text("print('{}')\n")
    with pytest.raises(MethodError, match="entrypoint refused"):
        await execute_method(
            None,  # type: ignore[arg-type]
            project_id=uuid.uuid4(),
            pack_id=uuid.uuid4(),
            pack_dir=str(pack_dir),
            method_spec={"slug": "m", "entrypoint": "../outside.py"},
            params={},
        )


def test_a_name_too_long_for_the_filesystem_is_a_pack_path_error(layout):
    """`is_file()` raises a plain OSError (ENAMETOOLONG) on 3.12; it must
    reach callers as PackPathError so validation reports it instead of 500."""
    pack, _ = layout
    with pytest.raises(PackPathError):
        resolve_pack_file(pack, "d/" + "x" * 400 + ".md")


def test_a_symlink_loop_is_a_pack_path_error(layout):
    """`resolve()` raises RuntimeError on a loop in 3.12, which no caller's
    `except OSError` would catch."""
    pack, _ = layout
    (pack / "a.md").symlink_to(pack / "b.md")
    (pack / "b.md").symlink_to(pack / "a.md")
    with pytest.raises(PackPathError):
        resolve_pack_file(pack, "a.md")


def test_doctrine_blocks_log_a_refused_stored_path(layout, caplog):
    pack_dir, secret = layout
    stored = SimpleNamespace(
        slug="legacy", version="0.1.0", source_path=str(pack_dir), manifest={"doctrine": [str(secret)]}
    )
    with caplog.at_level("WARNING", logger="tret.engine.context"):
        assert doctrine_blocks(stored, None) == []
    assert "refused" in caplog.text
    assert SECRET not in caplog.text
