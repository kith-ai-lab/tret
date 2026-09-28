"""Dockerfile.fly must not run the app as root.

docs/hardening.md's production checklist has always said "container/VM with a
non-root user" — the shipped Fly image did not honor its own checklist item.
This suite is a static check on the two files that fix that
(`Dockerfile.fly`, `fly-entrypoint.sh`), not a container-runtime test: actually
booting a container is exercised by hand (see the task notes), and asserting on
its structure here is what catches a future edit quietly reintroducing root —
e.g. someone "simplifying" the entrypoint back to a bare CMD, or dropping the
chown and leaving a Fly volume unwritable by the app user.

Two properties matter and are checked separately, because either can regress
without the other:

1. **The app never runs as root.** A non-root user exists, is created with a
   fixed uid/gid, and the image's ENTRYPOINT drops to it (via `setpriv`, which
   execs in place rather than forking, so the dropped-privilege process is
   PID 1 in every way that matters — see the boot-time verification in the
   task notes: `docker exec <container> cat /proc/1/status` reporting
   `Uid: 10001 ...`).
2. **The app can still write where it needs to.** `TRET_STORAGE_DIR` lives on
   a Fly volume mounted at `/data` after the image is built (fly.toml
   `[mounts]`), so a Dockerfile `RUN chown` cannot reach it — only the
   entrypoint, running at container start, can. Get this wrong and the image
   builds fine, boots fine, and then fails the first document upload.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_FLY = REPO_ROOT / "Dockerfile.fly"
ENTRYPOINT = REPO_ROOT / "fly-entrypoint.sh"
FLY_TOML = REPO_ROOT / "fly.toml.example"


def _dockerfile_text() -> str:
    return DOCKERFILE_FLY.read_text()


def _entrypoint_text() -> str:
    return ENTRYPOINT.read_text()


# ── a non-root user exists and is actually used ────────────────────────────────
def test_a_non_root_user_is_created_with_a_fixed_uid():
    text = _dockerfile_text()
    assert "groupadd" in text and "useradd" in text, (
        "Dockerfile.fly no longer creates a dedicated user — the image would fall "
        "back to running uvicorn as root, contradicting docs/hardening.md"
    )
    # A fixed id, not useradd's next-free default, so ownership set at build time
    # (chown tret:tret ...) and ownership set at boot (the entrypoint, on a
    # different "build") name the same numeric owner.
    assert "--gid 10001 tret" in text  # groupadd
    assert "--uid 10001 --gid tret" in text  # useradd


def test_the_entrypoint_drops_privileges_before_exec():
    text = _entrypoint_text()
    assert "setpriv" in text, (
        "fly-entrypoint.sh no longer uses setpriv — su/sudo fork a child instead "
        "of exec'ing in place, which leaves a root process sitting above the app "
        "and can break signal delivery to it"
    )
    assert "--reuid=tret" in text and "--regid=tret" in text
    # `exec setpriv ...`, not `setpriv ...` on its own: without `exec`, the shell
    # stays as PID 1 and setpriv's dropped-privilege child is not.
    assert "exec setpriv" in text


def test_the_image_entrypoint_is_the_privilege_drop_script():
    text = _dockerfile_text()
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in text, (
        "Dockerfile.fly does not run fly-entrypoint.sh as its ENTRYPOINT — "
        "without it nothing chowns the Fly volume or drops root before uvicorn"
    )
    assert "COPY fly-entrypoint.sh /entrypoint.sh" in text
    assert "chmod +x /entrypoint.sh" in text


def test_no_explicit_user_directive_undermines_the_root_to_tret_drop():
    """A Dockerfile `USER tret` would run entrypoint.sh itself as tret, and it
    needs root to chown the volume — so privilege-drop must happen in the
    entrypoint (via setpriv), not via a Dockerfile USER line.
    """
    text = _dockerfile_text()
    assert "\nUSER " not in text, (
        "Dockerfile.fly sets USER directly — entrypoint.sh would then run as "
        "that user and be unable to chown the Fly volume at /data"
    )


# ── build-time ownership: everything baked into the image ──────────────────────
def test_build_time_paths_are_owned_by_the_app_user():
    text = _dockerfile_text()
    assert "chown -R tret:tret /app /packs" in text, (
        "docs/, the pack tree, and the built frontend are COPYed as root by "
        "default; without a chown the non-root app user cannot read them"
    )
    # The chown must come after every COPY into /app or /packs specifically —
    # not necessarily every COPY in the file: fly-entrypoint.sh is deliberately
    # copied afterwards, since root (not tret) is what runs it.
    lines = text.splitlines()
    covered_copy_lines = [
        i for i, line in enumerate(lines) if line.startswith("COPY") and ("/app" in line or "/packs" in line)
    ]
    assert covered_copy_lines, "no COPY targets /app or /packs — nothing for the chown to cover"
    chown_line = next(i for i, line in enumerate(lines) if "chown -R tret:tret /app /packs" in line)
    assert all(i < chown_line for i in covered_copy_lines), (
        "chown -R tret:tret /app /packs runs before some COPY into /app or /packs"
    )


# ── boot-time ownership: the one path the Dockerfile cannot reach ──────────────
def test_entrypoint_fixes_ownership_of_the_fly_volume_before_dropping_root():
    """TRET_STORAGE_DIR sits on a Fly volume (fly.toml [mounts]) attached after
    the image is built. A freshly attached volume — or one written to by an
    older, fully-root image — is root-owned; only a boot-time step run before
    the privilege drop can fix that for the new non-root process.
    """
    text = _entrypoint_text()
    assert "chown -R tret:tret" in text
    assert "exec setpriv" in text
    # Ownership must be fixed before the privilege drop, not after — setpriv execs
    # in place, so nothing in the script runs once it has been reached. Matched
    # on the actual command strings, not bare keywords, since the file's own
    # comments mention both words ahead of where the commands appear.
    chown_pos = text.index("chown -R tret:tret")
    setpriv_pos = text.index("exec setpriv")
    assert chown_pos < setpriv_pos, "entrypoint drops privileges before fixing ownership"


def test_entrypoint_targets_the_configured_storage_dir_not_a_hardcoded_path():
    text = _entrypoint_text()
    assert "TRET_STORAGE_DIR" in text, (
        "entrypoint hardcodes a storage path instead of honoring "
        "TRET_STORAGE_DIR, so a deployment that overrides the setting would "
        "have its real storage directory left root-owned"
    )


def test_fly_toml_mount_matches_the_directory_the_entrypoint_fixes():
    """The entrypoint chowns dirname(TRET_STORAGE_DIR); that must be the Fly
    mount's destination, or the chown lands on a path Fly never actually mounts
    anything at.
    """
    mounts_block = FLY_TOML.read_text().split("[mounts]", 1)[1]
    destination = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in mounts_block.splitlines()
        if line.strip().startswith("destination")
    )
    dockerfile_storage_dir = next(
        line.split("=", 1)[1].strip()
        for line in _dockerfile_text().splitlines()
        if "TRET_STORAGE_DIR=" in line
    )
    assert dockerfile_storage_dir.startswith(destination + "/"), (
        f"fly.toml mounts the volume at {destination!r} but Dockerfile.fly sets "
        f"TRET_STORAGE_DIR={dockerfile_storage_dir!r}, which is not under it — "
        "the entrypoint's chown would miss the actual volume"
    )


# ── the entrypoint file itself is well-formed ───────────────────────────────────
def test_entrypoint_script_is_executable():
    import os
    import stat

    mode = ENTRYPOINT.stat().st_mode
    assert mode & stat.S_IXUSR, (
        "fly-entrypoint.sh is not executable (chmod +x) — docker can still exec "
        "it via the shebang in some configurations, but this is what the "
        "Dockerfile's own COPY + chmod +x is supposed to guarantee in the image"
    )
    # os import kept minimal and local: this is the only test that needs it.
    assert os.access(ENTRYPOINT, os.X_OK)


def test_entrypoint_has_a_shebang():
    first_line = _entrypoint_text().splitlines()[0]
    assert first_line.startswith("#!"), "fly-entrypoint.sh has no shebang"
