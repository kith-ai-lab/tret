"""egress_calls query fingerprint

Revision ID: c5e8a2d7b941
Revises: b3f8c2a6d914
Create Date: 2026-11-24 20:00:00.000000

- `egress_calls.query_length` / `egress_calls.query_sha256`: the length and a short
  SHA-256 prefix of the query string a research request carried. The query itself is
  still never stored; these make a query used as an exfiltration channel visible in
  the audit trail. NULL on rows written before this revision.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c5e8a2d7b941'
down_revision = 'b3f8c2a6d914'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('egress_calls', sa.Column('query_length', sa.Integer(), nullable=True))
    op.add_column('egress_calls', sa.Column('query_sha256', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('egress_calls', 'query_sha256')
    op.drop_column('egress_calls', 'query_length')
