"""workspaces.kind + workspaces.personal_owner_id

Revision ID: 3ffc3736279c
Revises: 35ed0d08502b
Create Date: 2026-08-25 09:05:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '3ffc3736279c'
down_revision = '35ed0d08502b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every existing workspace is a team workspace today — self-host's single
    # Default workspace, and every workspace a pre-tenancy deployment created.
    # The server default backfills them and stays on the column, matching the
    # model's default: a row written by code that predates this column should
    # read as 'team', never as a personal workspace nobody asked for.
    op.add_column(
        'workspaces', sa.Column('kind', sa.Text(), nullable=False, server_default='team')
    )
    # Nullable, UNIQUE: at most one personal workspace per user. No backfill —
    # nothing before multi-tenancy has a personal workspace.
    op.add_column('workspaces', sa.Column('personal_owner_id', sa.UUID(), nullable=True))
    op.create_unique_constraint(
        'uq_workspaces_personal_owner_id', 'workspaces', ['personal_owner_id']
    )
    op.create_foreign_key(
        'fk_workspaces_personal_owner_id_users',
        'workspaces',
        'users',
        ['personal_owner_id'],
        ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_workspaces_personal_owner_id_users', 'workspaces', type_='foreignkey')
    op.drop_constraint('uq_workspaces_personal_owner_id', 'workspaces', type_='unique')
    op.drop_column('workspaces', 'personal_owner_id')
    op.drop_column('workspaces', 'kind')
