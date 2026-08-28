"""harness_packs table (one harness, many packs)

Revision ID: 84cf41ced91f
Revises: 82169de695f6
Create Date: 2026-08-28 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '84cf41ced91f'
down_revision = '82169de695f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'harness_packs',
        sa.Column('harness_id', sa.UUID(), nullable=False),
        sa.Column('pack_id', sa.UUID(), nullable=False),
        sa.Column('position', sa.SmallInteger(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['harness_id'], ['harnesses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['pack_id'], ['packs.id']),
        sa.PrimaryKeyConstraint('harness_id', 'pack_id'),
    )
    # The PK covers harness_id lookups; pack-side lookups (uninstall's
    # reference check and severing) get their own index, same as every other
    # non-PK scan column in this schema.
    op.create_index('ix_harness_packs_pack_id', 'harness_packs', ['pack_id'])
    # Backfill: one position-0 row per harness that already named a pack.
    op.execute(
        """
        INSERT INTO harness_packs (harness_id, pack_id, position)
        SELECT id, pack_id, 0 FROM harnesses WHERE pack_id IS NOT NULL
        """
    )
    op.drop_column('harnesses', 'pack_id')


def downgrade() -> None:
    # Lossy by necessity: the single column can only hold the primary
    # (position 0) link, so any additional links a harness gained are
    # discarded here.
    op.add_column('harnesses', sa.Column('pack_id', sa.UUID(), nullable=True))
    op.execute(
        """
        UPDATE harnesses
        SET pack_id = harness_packs.pack_id
        FROM harness_packs
        WHERE harness_packs.harness_id = harnesses.id AND harness_packs.position = 0
        """
    )
    op.create_foreign_key(
        'harnesses_pack_id_fkey', 'harnesses', 'packs', ['pack_id'], ['id']
    )
    op.drop_table('harness_packs')
