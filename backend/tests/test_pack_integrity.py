"""Pack integrity pinning: the content hash and its verification."""
import pytest

from tret.packs import integrity
from tret.packs.integrity import (
    PackIntegrityError,
    cached_content_hash,
    iter_pack_entries,
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


# ── symlinks: the hole the pin used to have ──────────────────────────────────
# Excluding symlinks from the walk meant pinned pack code could be swapped
# without changing the hash. Each test below is one way to do that.
def _linked_pack(root, target_dir):
    """A pack whose method is a symlink to code living outside the pack."""
    pack = _pack(root)
    (target_dir).mkdir(parents=True, exist_ok=True)
    original = target_dir / "real.py"
    original.write_text("print('{\"rows\": [1]}')\n")
    (pack / "methods/linked.py").symlink_to(original)
    return pack, original


def test_a_symlink_is_part_of_the_hash(tmp_path):
    pack, _ = _linked_pack(tmp_path / "pack", tmp_path / "outside")
    entries = {e.rel: e.is_symlink for e in iter_pack_entries(pack)}
    assert entries["methods/linked.py"] is True
    assert entries["methods/m.py"] is False


def test_repointing_a_symlink_changes_the_hash(tmp_path):
    """The swap the old walk could not see: same link name, different code."""
    pack, _ = _linked_pack(tmp_path / "pack", tmp_path / "outside")
    pinned = pack_content_hash(pack)

    smuggled = tmp_path / "outside" / "smuggled.py"
    smuggled.write_text("import socket  # not what was reviewed\n")
    link = pack / "methods/linked.py"
    link.unlink()
    link.symlink_to(smuggled)

    assert pack_content_hash(pack) != pinned
    integrity.clear_cache()
    with pytest.raises(PackIntegrityError):
        verify_pack_integrity(pack, pinned, pack_label="p@0.1.0")


def test_the_cached_hash_also_notices_a_repointed_symlink(tmp_path):
    """The stat-signature shortcut must not paper over the swap.

    Both targets are written to the same length, so a signature that stat'ed
    through the link (or ignored the target) would look unchanged and the cache
    would keep serving the pre-swap hash.
    """
    pack, original = _linked_pack(tmp_path / "pack", tmp_path / "outside")
    integrity.clear_cache()
    before = cached_content_hash(pack)

    twin = tmp_path / "outside" / "twin.py"
    twin.write_text(original.read_text())  # identical bytes, identical size
    link = pack / "methods/linked.py"
    link.unlink()
    link.symlink_to(twin)

    assert cached_content_hash(pack) != before


def test_adding_or_removing_a_symlink_changes_the_hash(tmp_path):
    pack = _pack(tmp_path / "pack")
    before = pack_content_hash(pack)
    (pack / "alias.csv").symlink_to(pack / "data.csv")
    assert pack_content_hash(pack) != before
    (pack / "alias.csv").unlink()
    assert pack_content_hash(pack) == before


def test_a_symlinked_directory_is_pinned_without_being_followed(tmp_path):
    """A link to a directory is one entry, not a doorway into someone else's tree."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text("not part of the pack\n")
    pack = _pack(tmp_path / "pack")
    (pack / "vendor").symlink_to(elsewhere, target_is_directory=True)

    rels = [e.rel for e in iter_pack_entries(pack)]
    assert "vendor" in rels
    assert "vendor/secret.txt" not in rels  # never traversed

    pinned = pack_content_hash(pack)
    (elsewhere / "secret.txt").write_text("edited outside the pack\n")
    # Honest limit: content outside the pack directory is not pinned...
    assert pack_content_hash(pack) == pinned
    # ...but the link that reaches it is.
    (pack / "vendor").unlink()
    (pack / "vendor").symlink_to(tmp_path, target_is_directory=True)
    assert pack_content_hash(pack) != pinned


def test_a_symlink_cycle_does_not_hang_the_walk(tmp_path):
    pack = _pack(tmp_path / "pack")
    (pack / "loop").symlink_to(pack, target_is_directory=True)
    assert isinstance(pack_content_hash(pack), str)


def test_a_broken_symlink_is_still_pinned(tmp_path):
    pack = _pack(tmp_path / "pack")
    (pack / "dangling.csv").symlink_to(tmp_path / "never-existed.csv")
    before = pack_content_hash(pack)
    assert isinstance(before, str)
    (pack / "dangling.csv").unlink()
    (pack / "dangling.csv").symlink_to(tmp_path / "also-missing.csv")
    assert pack_content_hash(pack) != before


def test_a_symlink_is_not_confused_with_a_file_holding_its_target(tmp_path):
    """The tagged encoding: `x -> data.csv` must not hash like a file `x` whose
    contents are the string "data.csv"."""
    linked = _pack(tmp_path / "linked")
    (linked / "x").symlink_to(linked / "data.csv")
    plain = _pack(tmp_path / "plain")
    (plain / "x").write_text(str(linked / "data.csv"))
    assert pack_content_hash(linked) != pack_content_hash(plain)


def test_a_pack_without_symlinks_hashes_as_it_always_did(tmp_path):
    """Pinning symlinks must not re-pin every pack already installed.

    The encoding for regular files is unchanged, so this is the digest older
    tret releases stored for the same three files.
    """
    import hashlib

    pack = _pack(tmp_path)
    expected = hashlib.sha256()
    for rel in ("data.csv", "methods/m.py", "pack.yaml"):  # path-sorted
        data = (pack / rel).read_bytes()
        expected.update(len(rel.encode()).to_bytes(4, "big"))
        expected.update(rel.encode())
        expected.update(len(data).to_bytes(8, "big"))
        expected.update(data)
    assert pack_content_hash(pack) == expected.hexdigest()
