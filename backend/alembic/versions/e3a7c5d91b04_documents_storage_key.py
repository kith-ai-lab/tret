"""documents.storage_key: the BlobStore key behind each document

Revision ID: e3a7c5d91b04
Revises: d8b1f4a62c09
Create Date: 2026-11-06 09:00:00.000000

`documents.storage_path` is an absolute local path baked in when the row was
written. With an object-store backend (tret/storage, docs/storage.md) the bytes
are addressed by a key instead: `<sha256>-<filename>` for documents and
`packages/<finding_id>/<name>` for packaged outputs. Existing rows are
backfilled from their path; `storage_path` stays NOT NULL and populated.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e3a7c5d91b04'
down_revision = 'd8b1f4a62c09'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('storage_key', sa.Text(), nullable=True))
    # Normalise Windows separators first, then take the key from the last /packages/
    # on for package outputs and the basename for the rest (key_for_document's rule).
    op.execute("UPDATE documents SET storage_key = replace(storage_path, '\\', '/')")
    op.execute(
        "UPDATE documents SET storage_key = CASE "
        "WHEN storage_key LIKE '%/packages/%' "
        "THEN substring(storage_key from '.*/(packages/.*)$') "
        "ELSE regexp_replace(storage_key, '^.*/', '') END"
    )


def downgrade() -> None:
    op.drop_column('documents', 'storage_key')
