"""batches profile seam: profile, harness_id, parent_run_id, target label/task_input

Revision ID: d8b1f4a62c09
Revises: c4e9a2f71b38
Create Date: 2026-11-05 19:30:00.000000

`collections` / `collection_targets` become the generic batch tables (the ORM
classes are `Batch` / `BatchTarget`; the table names do not change). Existing
rows are disclosure collections: `profile` defaults to 'disclosure', `label` is
backfilled from `company`, `task_input` starts empty. `fiscal_year` becomes
nullable for profiles that have no fiscal year.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd8b1f4a62c09'
down_revision = 'c4e9a2f71b38'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'collections',
        sa.Column('profile', sa.Text(), nullable=False, server_default='disclosure'),
    )
    op.add_column('collections', sa.Column('harness_id', sa.UUID(), nullable=True))
    op.add_column('collections', sa.Column('parent_run_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_collections_harness_id', 'collections', 'harnesses', ['harness_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_foreign_key(
        'fk_collections_parent_run_id', 'collections', 'runs', ['parent_run_id'], ['id'],
        ondelete='SET NULL',
    )
    op.add_column(
        'collection_targets',
        sa.Column('label', sa.Text(), nullable=False, server_default=''),
    )
    op.add_column(
        'collection_targets',
        sa.Column(
            'task_input', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default='{}',
        ),
    )
    op.execute("UPDATE collection_targets SET label = company")
    op.alter_column('collection_targets', 'fiscal_year', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE collection_targets SET fiscal_year = 0 WHERE fiscal_year IS NULL")
    op.alter_column('collection_targets', 'fiscal_year', existing_type=sa.Integer(), nullable=False)
    op.drop_column('collection_targets', 'task_input')
    op.drop_column('collection_targets', 'label')
    op.drop_constraint('fk_collections_parent_run_id', 'collections', type_='foreignkey')
    op.drop_constraint('fk_collections_harness_id', 'collections', type_='foreignkey')
    op.drop_column('collections', 'parent_run_id')
    op.drop_column('collections', 'harness_id')
    op.drop_column('collections', 'profile')
