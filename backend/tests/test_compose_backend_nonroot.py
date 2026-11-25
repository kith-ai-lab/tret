"""The docker-compose backend image must not run the app as root.

Static checks, in the style of test_dockerfile_fly_nonroot.py: the compose path
(`backend/Dockerfile`) shares `fly-entrypoint.sh` with the Fly image, so it
chowns the named `storage` volume as root and then drops to uid 10001 `tret`.
Also pins the nginx body limit, since the default 1 MB 413s every document
upload on the compose path.
"""
from __future__ import annotations

import re
from pathlib import Path

from tret.services.documents import MAX_DOCUMENT_BYTES

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (REPO_ROOT / "backend" / "Dockerfile").read_text()
ENTRYPOINT = (REPO_ROOT / "fly-entrypoint.sh").read_text()
NGINX = (REPO_ROOT / "frontend" / "nginx.conf").read_text()


def test_backend_image_creates_the_same_fixed_user_as_fly():
    assert "--gid 10001 tret" in DOCKERFILE
    assert "--uid 10001 --gid tret" in DOCKERFILE
    assert "ENV HOME=/app" in DOCKERFILE


def test_backend_image_enters_through_the_privilege_drop_script():
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in DOCKERFILE
    assert "COPY fly-entrypoint.sh /entrypoint.sh" in DOCKERFILE
    # A USER line would run the entrypoint unprivileged and it could not chown.
    assert "\nUSER " not in DOCKERFILE


def test_app_dir_is_chowned_after_every_copy_into_it():
    lines = DOCKERFILE.splitlines()
    chown = next(i for i, ln in enumerate(lines) if "chown -R tret:tret /app" in ln)
    copies = [i for i, ln in enumerate(lines) if ln.startswith("COPY") and "./" in ln and "entrypoint" not in ln]
    assert copies and all(i < chown for i in copies)


def test_entrypoint_chowns_before_dropping_and_tolerates_non_root():
    assert ENTRYPOINT.index("chown -R tret:tret") < ENTRYPOINT.index("exec setpriv")
    assert '[ "$(id -u)" != "0" ]' in ENTRYPOINT


def test_nginx_allows_the_largest_legitimate_upload():
    m = re.search(r"client_max_body_size\s+(\d+)([km])\s*;", NGINX)
    assert m, "nginx.conf has no client_max_body_size: the 1 MB default 413s uploads"
    limit = int(m.group(1)) * (1024 if m.group(2) == "k" else 1024 * 1024)
    assert limit > MAX_DOCUMENT_BYTES + 64 * 1024
    assert limit <= 2 * MAX_DOCUMENT_BYTES, "limit should sit just above the backend cap"
