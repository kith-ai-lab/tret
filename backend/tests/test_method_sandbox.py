"""The hardened method subprocess: it still runs, and it refuses a drifted pack.

No DB — `execute_method` only needs a session that can get/add/flush, so a fake
stands in (the same stance as tests/test_methods.py running scripts directly).
"""
import uuid

import pytest

from bench.packs import integrity
from bench.packs.integrity import pack_content_hash
from bench.services import methods
from bench.services.methods import MethodError, execute_method, network_isolation_prefix

ECHO_METHOD = """
import json
import sys

payload = json.load(sys.stdin)
json.dump({"rows": [{"got": payload["params"].get("n", 0)}]}, sys.stdout)
"""


class FakePack:
    def __init__(self, content_hash):
        self.slug = "sandbox"
        self.version = "0.1.0"
        self.content_hash = content_hash


class FakeSession:
    """Enough AsyncSession surface for execute_method with no declared inputs."""

    def __init__(self, pack):
        self.pack = pack
        self.added = []

    async def get(self, _model, _pk):
        return self.pack

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _pack_dir(tmp_path, source=ECHO_METHOD):
    (tmp_path / "methods").mkdir(parents=True)
    (tmp_path / "methods/echo.py").write_text(source)
    (tmp_path / "pack.yaml").write_text("pack: sandbox\nversion: 0.1.0\ndisplay_name: Sandbox\n")
    return tmp_path


SPEC = {"slug": "echo", "entrypoint": "methods/echo.py", "inputs": [], "timeout_seconds": 30}


async def _run(db, pack_dir, spec=None, params=None):
    return await execute_method(
        db,
        project_id=uuid.uuid4(),
        pack_id=uuid.uuid4(),
        pack_dir=str(pack_dir),
        method_spec=spec or SPEC,
        params=params or {"n": 7},
    )


@pytest.fixture(autouse=True)
def _fresh_sandbox_state():
    integrity.clear_cache()
    methods._isolation_prefix = None
    yield
    integrity.clear_cache()
    methods._isolation_prefix = None


async def test_method_runs_under_the_hardened_subprocess(tmp_path):
    pack_dir = _pack_dir(tmp_path)
    db = FakeSession(FakePack(pack_content_hash(pack_dir)))
    record = await _run(db, pack_dir)
    assert record.status == "completed"
    assert record.output == [{"got": 7}]
    assert record.row_count == 1
    assert record.output_hash


async def test_tampered_pack_fails_loudly(tmp_path):
    pack_dir = _pack_dir(tmp_path)
    pinned = pack_content_hash(pack_dir)
    integrity.clear_cache()
    (pack_dir / "methods/echo.py").write_text(
        'import json, sys\njson.dump({"rows": [{"got": 999}]}, sys.stdout)\n'
    )
    db = FakeSession(FakePack(pinned))
    with pytest.raises(MethodError, match="integrity check"):
        await _run(db, pack_dir)
    # The failure is still recorded as a method_run for audit.
    assert db.added and db.added[0].status == "failed"
    assert "Reinstall the pack" in db.added[0].error


async def test_dataset_edit_also_breaks_integrity(tmp_path):
    pack_dir = _pack_dir(tmp_path)
    (pack_dir / "data.csv").write_text("a\n1\n")
    pinned = pack_content_hash(pack_dir)
    integrity.clear_cache()
    (pack_dir / "data.csv").write_text("a\n2\n")
    with pytest.raises(MethodError, match="integrity check"):
        await _run(FakeSession(FakePack(pinned)), pack_dir)


async def test_legacy_unpinned_pack_still_runs(tmp_path):
    pack_dir = _pack_dir(tmp_path)
    record = await _run(FakeSession(FakePack(None)), pack_dir)
    assert record.status == "completed"


async def test_method_failure_is_reported_with_stderr(tmp_path):
    pack_dir = _pack_dir(tmp_path, "raise SystemExit('boom')\n")
    db = FakeSession(FakePack(None))
    with pytest.raises(MethodError, match="boom"):
        await _run(db, pack_dir)


async def test_bad_output_shape_is_rejected(tmp_path):
    pack_dir = _pack_dir(tmp_path, "print('not json')\n")
    with pytest.raises(MethodError, match="not valid"):
        await _run(FakeSession(FakePack(None)), pack_dir)


async def test_timeout_is_enforced(tmp_path):
    pack_dir = _pack_dir(tmp_path, "import time\ntime.sleep(30)\n")
    spec = {**SPEC, "timeout_seconds": 0.5}
    with pytest.raises(MethodError, match="timed out"):
        await _run(FakeSession(FakePack(None)), pack_dir, spec=spec)


async def test_network_isolation_disabled_yields_no_prefix(monkeypatch):
    monkeypatch.setattr(methods.get_settings(), "methods_network_isolation", False)
    assert await network_isolation_prefix() == []


async def test_network_isolation_is_probed_once(monkeypatch):
    monkeypatch.setattr(methods.get_settings(), "methods_network_isolation", True)
    first = await network_isolation_prefix()
    assert first == await network_isolation_prefix()  # cached, no re-probe
    # Off Linux (or without a usable `unshare`) the documented fallback is
    # "no isolation", never a hard failure.
    assert first == [] or first[-2:] == ["--net", "--"]
