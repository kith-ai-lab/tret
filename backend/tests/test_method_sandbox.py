"""The hardened method subprocess: it still runs, and it refuses a drifted pack.

No DB — `execute_method` only needs a session that can get/add/flush, so a fake
stands in (the same stance as tests/test_methods.py running scripts directly).
"""
import asyncio
import logging
import sys
import uuid

import pytest

from tret.packs import integrity
from tret.packs.integrity import pack_content_hash
from tret.services import methods
from tret.services.methods import MethodError, execute_method, network_isolation_prefix

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


async def test_output_over_the_size_cap_is_killed_mid_stream(tmp_path, monkeypatch):
    """The cap must stop the writer, not merely reject it afterwards.

    stdout used to be buffered whole by `communicate()` and measured after, so a
    method emitting gigabytes exhausted the process's memory instead of being
    capped. This method writes past the cap and then sleeps well past the run's
    timeout: capping at the cap ends it immediately, while buffering-then-checking
    waits for the child and reports a timeout instead.
    """
    monkeypatch.setattr(methods, "MAX_OUTPUT_BYTES", 1000)
    pack_dir = _pack_dir(
        tmp_path,
        "import sys, time\nsys.stdout.write('x' * 400_000)\nsys.stdout.flush()\ntime.sleep(30)\n",
    )
    with pytest.raises(MethodError, match="exceeds size cap"):
        await _run(FakeSession(FakePack(None)), pack_dir, spec={**SPEC, "timeout_seconds": 2})


async def test_output_exactly_at_the_size_cap_is_accepted(tmp_path, monkeypatch):
    """The cap is a limit, not an off-by-one: output at the cap still counts."""
    body = '{"rows": [{"pad": "%s"}]}'
    padding = "p" * 200
    monkeypatch.setattr(methods, "MAX_OUTPUT_BYTES", len(body % padding))
    pack_dir = _pack_dir(tmp_path, f"import sys\nsys.stdout.write('{body % padding}')\n")
    record = await _run(FakeSession(FakePack(None)), pack_dir)
    assert record.status == "completed"
    assert record.output == [{"pad": padding}]


async def test_stderr_beyond_its_cap_does_not_stall_the_method(tmp_path, monkeypatch):
    """A chatty method still returns its rows: stderr is drained, not blocked."""
    monkeypatch.setattr(methods, "MAX_STDERR_BYTES", 256)
    pack_dir = _pack_dir(
        tmp_path,
        "import json, sys\n"
        "sys.stderr.write('noise ' * 50_000)\n"
        'json.dump({"rows": [{"got": 7}]}, sys.stdout)\n',
    )
    record = await _run(
        FakeSession(FakePack(None)), pack_dir, spec={**SPEC, "timeout_seconds": 10}
    )
    assert record.status == "completed"
    assert record.output == [{"got": 7}]


async def test_network_isolation_disabled_yields_no_prefix(monkeypatch):
    monkeypatch.setattr(methods.get_settings(), "methods_network_isolation", False)
    assert await network_isolation_prefix() == []


async def test_network_isolation_is_probed_once(monkeypatch):
    monkeypatch.setattr(methods.get_settings(), "methods_network_isolation", True)
    probes = []
    real_exec = asyncio.create_subprocess_exec

    async def counting_exec(*args, **kwargs):
        probes.append(args)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_exec)
    first = await network_isolation_prefix()
    second = await network_isolation_prefix()
    assert second is first  # cached: the second call spawns nothing
    assert len(probes) <= 1  # 1 on Linux (the `unshare --net true` probe), 0 elsewhere
    # Off Linux (or without a usable `unshare`) the documented fallback is
    # "no isolation", never a hard failure.
    assert first == [] or first[-2:] == ["--net", "--"]


@pytest.mark.skipif(
    sys.platform == "linux", reason="the off-Linux fallback only exists off Linux"
)
async def test_off_linux_isolation_falls_back_but_says_so(monkeypatch, caplog):
    """Where isolation cannot work, asking for it must warn — silence is the bug.

    This is the honest half of the pair below: on this platform there is no
    network boundary to assert, so what is asserted is that the operator who
    asked for one is told they did not get it.
    """
    monkeypatch.setattr(methods.get_settings(), "methods_network_isolation", True)
    with caplog.at_level(logging.WARNING, logger="tret.methods"):
        assert await network_isolation_prefix() == []
    assert "WITHOUT network isolation" in caplog.text
    assert sys.platform in caplog.text


NETWORK_PROBE = """
import json
import socket
import sys

try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
    reachable = True
except OSError:
    reachable = False
json.dump({"rows": [{"reachable": reachable}]}, sys.stdout)
"""


@pytest.mark.skipif(sys.platform != "linux", reason="`unshare --net` is Linux-only")
async def test_isolated_method_cannot_reach_the_network(tmp_path, monkeypatch):
    """Where isolation is supposed to work, prove a method really has no network.

    Skipped honestly when the host cannot create a network namespace (no
    `unshare`, or no permission) — the same condition the runtime warns about.
    """
    monkeypatch.setattr(methods.get_settings(), "methods_network_isolation", True)
    prefix = await network_isolation_prefix()
    if not prefix:
        pytest.skip("unshare --net is not usable here; the runtime warns and falls back")
    assert prefix[0].endswith("unshare") and prefix[1:] == ["--net", "--"]

    pack_dir = _pack_dir(tmp_path, NETWORK_PROBE)
    record = await _run(
        FakeSession(FakePack(None)), pack_dir, spec={**SPEC, "timeout_seconds": 20}
    )
    assert record.status == "completed"
    assert record.output == [{"reachable": False}], (
        "a method opened a socket inside what should be an empty network namespace"
    )
