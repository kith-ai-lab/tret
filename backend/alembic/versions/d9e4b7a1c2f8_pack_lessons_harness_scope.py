"""pack_lessons harness scope

Revision ID: d9e4b7a1c2f8
Revises: c7d2e9a4b6f1
Create Date: 2026-11-12 12:00:00.000000

A lesson may now be scoped to a harness instead of a pack (batch-review lessons,
proposed by the evaluator and approved by a person). Exactly one of pack_slug /
harness_id is set, enforced by a CHECK constraint.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd9e4b7a1c2f8'
down_revision = 'c7d2e9a4b6f1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('pack_lessons', sa.Column('harness_id', sa.UUID(), nullable=True))
    op.add_column('pack_lessons', sa.Column('proposed_by_evaluation_id', sa.UUID(), nullable=True))
    op.alter_column('pack_lessons', 'pack_slug', existing_type=sa.Text(), nullable=True)
    op.create_foreign_key(
        'fk_pack_lessons_harness_id', 'pack_lessons', 'harnesses',
        ['harness_id'], ['id'], ondelete='CASCADE',
    )
    op.create_foreign_key(
        'fk_pack_lessons_proposed_by_evaluation_id', 'pack_lessons', 'batch_evaluations',
        ['proposed_by_evaluation_id'], ['id'], ondelete='SET NULL',
    )
    op.create_check_constraint(
        'ck_pack_lessons_one_scope', 'pack_lessons',
        '(pack_slug IS NOT NULL) <> (harness_id IS NOT NULL)',
    )
    op.create_unique_constraint(
        'uq_pack_lessons_workspace_harness_ordinal', 'pack_lessons',
        ['workspace_id', 'harness_id', 'ordinal'],
    )
    op.create_index(
        'ix_pack_lessons_workspace_harness', 'pack_lessons', ['workspace_id', 'harness_id'],
        unique=False,
    )
    op.create_index(
        'ix_pack_lessons_proposed_by_evaluation', 'pack_lessons', ['proposed_by_evaluation_id'],
        unique=False,
    )


def downgrade() -> None:
    op.execute('DELETE FROM pack_lessons WHERE harness_id IS NOT NULL')
    op.drop_index('ix_pack_lessons_proposed_by_evaluation', table_name='pack_lessons')
    op.drop_index('ix_pack_lessons_workspace_harness', table_name='pack_lessons')
    op.drop_constraint('uq_pack_lessons_workspace_harness_ordinal', 'pack_lessons', type_='unique')
    op.drop_constraint('ck_pack_lessons_one_scope', 'pack_lessons', type_='check')
    op.drop_constraint('fk_pack_lessons_proposed_by_evaluation_id', 'pack_lessons', type_='foreignkey')
    op.drop_constraint('fk_pack_lessons_harness_id', 'pack_lessons', type_='foreignkey')
    op.alter_column('pack_lessons', 'pack_slug', existing_type=sa.Text(), nullable=False)
    op.drop_column('pack_lessons', 'proposed_by_evaluation_id')
    op.drop_column('pack_lessons', 'harness_id')
