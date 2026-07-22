from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.api.auth import current_user
from bench.config import get_settings
from bench.db.engine import get_db
from bench.db.models import Dataset, DatasetRow, Document, Project, User
from bench.services.documents import extract_text

router = APIRouter(prefix="/api", tags=["documents"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _doc_out(d: Document) -> dict:
    return {
        "id": str(d.id),
        "project_id": str(d.project_id),
        "filename": d.filename,
        "content_type": d.content_type,
        "byte_size": d.byte_size,
        "extraction_status": d.extraction_status,
        "meta": d.meta,
        "text_chars": len(d.extracted_text or ""),
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


@router.post("/documents")
async def upload_document(
    file: UploadFile, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (25MB max)")
    project = (await db.execute(select(Project))).scalars().first()

    sha = hashlib.sha256(data).hexdigest()
    storage_dir = Path(get_settings().storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{sha}-{file.filename}"
    storage_path.write_bytes(data)

    doc = Document(
        project_id=project.id,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        byte_size=len(data),
        storage_path=str(storage_path),
        sha256=sha,
        uploaded_by=user.id,
    )
    try:
        text, meta = extract_text(doc.filename, data)
        doc.extracted_text = text
        doc.meta = meta
        doc.extraction_status = "done"
    except ValueError as e:
        doc.extraction_status = "failed"
        doc.meta = {"error": str(e)}
    db.add(doc)
    await db.commit()
    return _doc_out(doc)


@router.get("/documents")
async def list_documents(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    docs = (
        (await db.execute(select(Document).order_by(Document.created_at.desc()).limit(200)))
        .scalars()
        .all()
    )
    return [_doc_out(d) for d in docs]


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    d = await db.get(Document, document_id)
    if d is None:
        raise HTTPException(404, "Document not found")
    return {**_doc_out(d), "extracted_text": (d.extracted_text or "")[:100_000]}


@router.get("/datasets")
async def list_datasets(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    datasets = (await db.execute(select(Dataset).order_by(Dataset.name))).scalars().all()
    return [
        {
            "id": str(ds.id),
            "name": ds.name,
            "columns": ds.schema_json.get("columns", []),
            "row_count": ds.row_count,
            "pack_seeded": ds.pack_id is not None,
        }
        for ds in datasets
    ]


@router.get("/datasets/{dataset_id}/rows")
async def dataset_rows(
    dataset_id: uuid.UUID,
    limit: int = 20,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(DatasetRow)
            .where(DatasetRow.dataset_id == dataset_id)
            .order_by(DatasetRow.row_index)
            .limit(min(limit, 200))
        )
    ).scalars().all()
    return [r.data for r in rows]
