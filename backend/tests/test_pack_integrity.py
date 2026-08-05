"""Pack integrity pinning: the content hash and its verification."""
import pytest

from bench.packs import integrity
from bench.packs.integrity import (
    PackIntegrityError,
    cached_content_hash,
    iter_pack_files,
    pack_content_hash,
    verify_pack_integrity,
)


def _pack(root):
    (root / "methods").mkdir(parents=True)
    (root / "methods/m.py").write_text("import json\nprint(json.dumps({'rows': []}))\n")
    (root / "pack.yaml").write_text("pack: p\nversion: 0.1.0\ndisplay_name: P\n")
    (root / "data.csv").write_text("a,b\n1,2\n")
    return root


def test_hash_is_stable_and_covers_every_file(tmp_path):
    pack = _pack(tmp_path)
    first = pack_content_hash(pack)
    assert first == pack_content_hash(pack)
    assert {p.name for p in iter_pack_files(pack)} == {"m.py", "pack.yaml", "data.csv"}


def test_hash_changes_when_any_file_changes(tmp_path):
    pack = _pack(tmp_path)
    before = pack_content_hash(pack)
    (pack / "data.csv").write_text("a,b\n1,3\n")
    assert pack_content_hash(pack) != before


def test_hash_changes_when_a_file_is_added_or_removed(tmp_path):
    pack = _pack(tmp_path)
    before = pack_content_hash(pack)
    (pack / "methods/extra.py").write_text("# new\n")
    added = pack_content_hash(pack)
    assert added != before
    (pack / "methods/extra.py").unlink()
    assert pack_content_hash(pack) == before


def test_hash_depends_on_path_not_just_bytes(tmp_path):
    a = _pack(tmp_path / "a")
    b = _pack(tmp_path / "b")
    (a / "one.txt").write_text("same")
    (b / "two.txt").write_text("same")
    assert pack_content_hash(a) != pack_content_hash(b)


def test_build_artefacts_are_ignored(tmp_path):
    pack = _pack(tmp_path)
    before = pack_content_hash(pack)
    (pack / "methods/__pycache__").mkdir()
    (pack / "methods/__pycache__/m.cpython-312.pyc").write_bytes(b"\x00binary")
    (pack / ".DS_Store").write_bytes(b"junk")
    assert pack_content_hash(pack) == before


def test_verify_passes_for_untouched_pack(tmp_path):
    pack = _pack(tmp_path)
    integrity.clear_cache()
    verify_pack_integrity(pack, pack_content_hash(pack), pack_label="p@0.1.0")


def test_verify_fails_loudly_for_a_tampered_method(tmp_path):
    pack = _pack(tmp_path)
    pinned = pack_content_hash(pack)
    integrity.clear_cache()
    (pack / "methods/m.py").write_text("import socket  # smuggled in after install\n")
    with pytest.raises(PackIntegrityError) as excinfo:
        verify_pack_integrity(pack, pinned, pack_label="p@0.1.0")
    message = str(excinfo.value)
    assert "p@0.1.0" in message
    assert pinned[:16] in message
    assert "Reinstall the pack" in message


def test_verify_is_a_no_op_for_legacy_unpinned_packs(tmp_path):
    verify_pack_integrity(_pack(tmp_path), None, pack_label="p@0.1.0")


def test_cached_hash_notices_edits(tmp_path):
    pack = _pack(tmp_path)
    integrity.clear_cache()
    before = cached_content_hash(pack)
    assert cached_content_hash(pack) == before  # served from cache
    (pack / "data.csv").write_text("a,b\n9,9\n")
    assert cached_content_hash(pack) != before
