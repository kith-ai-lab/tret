"""conversation connected folders

Revision ID: e5b8c2d7f013
Revises: d3a7b1c6e904
Create Date: 2026-11-18 09:00:00.000000

`conversations.connected_folders`: OneDrive/SharePoint folders attached to a chat
session, a JSON list of {drive_id, item_id, name, path, web_url}; `[]` when none.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e5b8c2d7f013'
down_revision = 'd3a7b1c6e904'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'conversations',
        sa.Column(
            'connected_folders',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default='[]',
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('conversations', 'connected_folders')
