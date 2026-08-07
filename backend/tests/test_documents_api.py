"""`POST /api/documents`: the upload path, which is where third-party bytes and
a third-party filename enter bench.

The interesting assertions are the refusals. An upload endpoint has three ways to
be dangerous — it can be talked into using memory it does not have, into writing
where it should not, and into spending unbounded time parsing — and each is
tested here rather than reasoned about:

* **size** — the cap is enforced while the body is consumed, so the endpoint
  never holds an oversized body (the test proves it by counting how much of the
  stream was read before the 413, which fails if anyone reinstates a single
  `await file.read()`);
* **the stored name** — traversal, absolute paths, NULs, dot-names and 4KB names
  all land inside the storage directory under a name derived by sanitisation;
* **extraction** — output truncated, wall time capped, and a parser that throws
  produces a `failed` row instead of a 500.

The database is a fake: nothing here is about SQL.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bench.api import documents
from bench.api.auth import current_user
from bench.api.documents import safe_content_type, safe_filename
from bench.config import get_settings
from bench.db.engine import get_db
from bench.db.models import Document, Project, User

PROJECT = Project(id=uuid.uuid4(), workspace_id=uuid.uuid4(), name="P")
UPLOADER = User(id=uuid.uuid4(), email="a@example.com", display_name="A", role="analyst")


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self, projects=(PROJECT,)):
        self.projects = list(projects)
        self.added: list = []
        self.commits = 0

    async def execute(self, _stmt):
        return FakeResult(self.projects)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    @property
    def document(self) -> Document:
        docs = [d for d in self.added if isinstance(d, Document)]
        assert len(docs) == 1, f"expected exactly one document row, got {len(docs)}"
        return docs[0]


@pytest.fixture
def db() -> FakeSession:
    return FakeSession()


@pytest.fixture
def storage(tmp_path, monkeypatch):
    directory = tmp_path / "storage"
    monkeypatch.setattr(get_settings(), "storage_dir", str(directory))
    return directory


@pytest.fixture
def client(db, storage) -> TestClient:
    app = FastAPI()
    app.include_router(documents.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: UPLOADER
    return TestClient(app)


def upload(client, name="notes.txt", content=b"hello", content_type="text/plain"):
    return client.post("/api/documents", files={"file": (name, content, content_type)})


def stored_names(storage) -> list[str]:
    return sorted(p.name for p in storage.iterdir())


# ── the happy path, so the refusals below mean something ─────────────────────
def test_a_text_document_is_stored_hashed_and_extracted(client, db, storage):
    response = upload(client, "notes.txt", b"line one\nline two")
    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "notes.txt"
    assert body["content_type"] == "text/plain"
    assert body["byte_size"] == 17
    assert body["extraction_status"] == "done"
    assert body["text_chars"] == 17

    doc = db.document
    assert doc.uploaded_by == UPLOADER.id
    assert doc.project_id == PROJECT.id
    assert doc.extracted_text == "line one\nline two"
    assert db.commits == 1

    files = stored_names(storage)
    assert files == [f"{doc.sha256}-notes.txt"]
    assert (storage / files[0]).read_bytes() == b"line one\nline two"


def test_the_sha256_is_of_the_bytes_as_received(client, db):
    import hashlib

    content = b"x" * (documents.UPLOAD_CHUNK_BYTES + 1234)  # spans chunk boundaries
    upload(client, "big.txt", content)
    assert db.document.sha256 == hashlib.sha256(content).hexdigest()
    assert db.document.byte_size == len(content)


def test_an_upload_requires_a_session(db, storage):
    """No dependency override for current_user: the real one runs and refuses."""
    app = FastAPI()
    app.include_router(documents.router)
    app.dependency_overrides[get_db] = lambda: db
    anonymous = TestClient(app)
    assert upload(anonymous).status_code == 401
    assert db.added == []


# ── the size cap ─────────────────────────────────────────────────────────────
def test_an_oversized_upload_is_refused(client, db, storage, monkeypatch):
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 1024)
    monkeypatch.setattr(documents, "UPLOAD_CHUNK_BYTES", 256)
    monkeypatch.setattr(documents, "MULTIPART_OVERHEAD_ALLOWANCE", 64 * 1024)  # skip the precheck

    response = upload(client, "big.bin", b"z" * 4096)
    assert response.status_code == 413
    assert "too large" in response.json()["detail"].lower()
    assert db.added == []
    assert db.commits == 0
    assert stored_names(storage) == []  # not even a partial file left behind


def test_the_cap_is_enforced_before_the_whole_body_is_read(client, storage, monkeypatch):
    """The regression that matters: the cap must not be a post-mortem.

    The old implementation did `data = await file.read()` and measured
    afterwards, so a body far larger than memory was in memory before it was
    judged. This counts the bytes actually pulled from the stream: the endpoint
    must stop shortly after the cap, not at the end of the body.
    """
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 1024)
    monkeypatch.setattr(documents, "UPLOAD_CHUNK_BYTES", 256)
    monkeypatch.setattr(documents, "MULTIPART_OVERHEAD_ALLOWANCE", 1024 * 1024)

    read_total = 0
    original = documents.UploadFile.read

    async def counting_read(self, size=-1):
        nonlocal read_total
        assert size > 0, "the body must be read in bounded chunks, never all at once"
        chunk = await original(self, size)
        read_total += len(chunk)
        return chunk

    monkeypatch.setattr(documents.UploadFile, "read", counting_read)
    assert upload(client, "big.bin", b"z" * 200_000).status_code == 413
    assert read_total <= 1024 + 256, f"read {read_total} bytes to reject a 1024-byte cap"


def test_a_declared_content_length_over_the_cap_is_refused_before_any_work(
    client, storage, monkeypatch
):
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 1024)
    monkeypatch.setattr(documents, "MULTIPART_OVERHEAD_ALLOWANCE", 512)

    read_calls = 0
    original = documents.UploadFile.read

    async def counting_read(self, size=-1):
        nonlocal read_calls
        read_calls += 1
        return await original(self, size)

    monkeypatch.setattr(documents.UploadFile, "read", counting_read)
    assert upload(client, "big.bin", b"z" * 8192).status_code == 413
    assert read_calls == 0
    assert not storage.exists() or stored_names(storage) == []


def test_a_file_at_the_cap_is_accepted(client, db, monkeypatch):
    """The multipart envelope must not cost the client part of its allowance."""
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 4096)
    assert upload(client, "at-cap.txt", b"z" * 4096).status_code == 200
    assert db.document.byte_size == 4096


def test_an_empty_upload_is_refused(client, db, storage):
    response = upload(client, "empty.txt", b"")
    assert response.status_code == 422
    assert "empty" in response.json()["detail"].lower()
    assert db.added == []
    assert stored_names(storage) == []


# ── the stored filename ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        ("../../../../etc/cron.d/pwn", "pwn"),
        ("../../evil.txt", "evil.txt"),
        ("/etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\evil.dll", "evil.dll"),
        ("dir/sub/report.pdf", "report.pdf"),
        ("..", "upload"),
        (".", "upload"),
        ("...", "upload"),
        ("", "upload"),
        (None, "upload"),
        (".bashrc", "bashrc"),
        ("nul\x00byte.txt", "nulbyte.txt"),
        ("re;port$(id).csv", "re_port_(id).csv"),
        ("quote\"and'.txt", "quote_and_.txt"),
        ("<script>.html", "_script_.html"),
        ("space   name.txt", "space   name.txt"),
        ("new\nline\ttab.txt", "new_line_tab.txt"),
        ("right‮ovirrep.txt", "right_ovirrep.txt"),  # bidi override
        # Word characters in any script survive: sanitising must not mean ASCII-only.
        ("Ünïcodé.txt", "Ünïcodé.txt"),
        ("年次報告.pdf", "年次報告.pdf"),
    ],
)
def test_filenames_are_reduced_to_a_safe_basename(sent, expected):
    assert safe_filename(sent) == expected


def test_a_very_long_filename_is_truncated_but_keeps_its_extension():
    name = safe_filename("a" * 4000 + ".csv")
    assert name.endswith(".csv")
    assert len(name) <= 120


def test_a_traversal_filename_lands_inside_the_storage_directory(client, db, storage):
    """End to end: the escape attempt is stored as an ordinary file, in place."""
    escape = storage.parent / "etc"
    response = upload(client, "../../etc/cron.d/pwn", b"payload")
    assert response.status_code == 200
    assert response.json()["filename"] == "pwn"

    doc = db.document
    assert doc.filename == "pwn"
    assert stored_names(storage) == [f"{doc.sha256}-pwn"]
    assert doc.storage_path.startswith(str(storage.resolve()))
    assert not escape.exists()


def test_two_uploads_of_the_same_bytes_share_one_stored_file(client, storage):
    assert upload(client, "a.txt", b"same").status_code == 200
    assert upload(client, "a.txt", b"same").status_code == 200
    assert len(stored_names(storage)) == 1


def test_no_partial_files_are_left_in_the_storage_directory(client, storage):
    assert upload(client, "a.txt", b"content").status_code == 200
    assert not [n for n in stored_names(storage) if n.startswith(".incoming-")]


# ── the declared content type ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        ("text/plain", "text/plain"),
        ("TEXT/CSV", "text/csv"),
        ("text/plain; charset=utf-8", "text/plain"),
        ("application/pdf", "application/pdf"),
        ("", "application/octet-stream"),
        (None, "application/octet-stream"),
        ("not a media type", "application/octet-stream"),
        ("text/html<script>alert(1)</script>", "application/octet-stream"),
        ("text/plain\r\nX-Injected: 1", "application/octet-stream"),
        ("a/" + "b" * 500, "application/octet-stream"),
    ],
)
def test_content_types_are_validated_not_echoed(sent, expected):
    assert safe_content_type(sent) == expected


def test_extraction_dispatches_on_the_extension_not_the_declared_type(client, db):
    """A .txt announced as a PDF is still read as text: the client does not choose."""
    response = upload(client, "notes.txt", b"plain words", content_type="application/pdf")
    assert response.status_code == 200
    assert db.document.extraction_status == "done"
    assert db.document.extracted_text == "plain words"


# ── extraction bounds ────────────────────────────────────────────────────────
def test_an_unsupported_type_is_stored_with_a_failed_extraction(client, db, storage):
    response = upload(client, "photo.jpeg", b"\xff\xd8\xff\xe0nonsense")
    assert response.status_code == 200
    body = response.json()
    assert body["extraction_status"] == "failed"
    assert "Unsupported file type" in body["meta"]["error"]
    assert body["text_chars"] == 0
    assert len(stored_names(storage)) == 1  # the bytes are kept regardless


def test_a_parser_that_throws_does_not_fail_the_upload(client, db, monkeypatch):
    def exploding(_filename, _data):
        raise RuntimeError("pypdf found something it did not like")

    monkeypatch.setattr(documents, "extract_text", exploding)
    response = upload(client, "hostile.pdf", b"%PDF-1.4 nonsense")
    assert response.status_code == 200
    assert db.document.extraction_status == "failed"
    assert db.document.meta["error"] == "text extraction failed: RuntimeError"
    assert db.document.extracted_text is None


def test_extraction_output_is_truncated(client, db, monkeypatch):
    monkeypatch.setattr(documents, "MAX_EXTRACTED_CHARS", 100)
    response = upload(client, "long.txt", b"y" * 5000)
    assert response.status_code == 200
    doc = db.document
    assert len(doc.extracted_text) == 100
    assert doc.meta["truncated"] is True
    assert doc.meta["extracted_chars"] == 100
    assert doc.byte_size == 5000  # the stored bytes are not truncated
    assert response.json()["text_chars"] == 100


def test_extraction_that_hangs_is_abandoned(client, db, monkeypatch):
    import time

    monkeypatch.setattr(documents, "EXTRACTION_TIMEOUT_SECONDS", 0.05)

    def slow(_filename, _data):
        time.sleep(5)
        raise AssertionError("should have been abandoned")

    monkeypatch.setattr(documents, "extract_text", slow)
    response = upload(client, "slow.txt", b"content")
    assert response.status_code == 200
    assert db.document.extraction_status == "failed"
    assert "timed out" in db.document.meta["error"]


def test_a_csv_is_extracted_with_its_shape_recorded(client, db):
    upload(client, "rows.csv", b"region,value\neu,10\nus,20\n", content_type="text/csv")
    doc = db.document
    assert doc.extraction_status == "done"
    assert doc.meta == {"rows": 3, "columns": ["region", "value"]}
    assert "eu | 10" in doc.extracted_text
