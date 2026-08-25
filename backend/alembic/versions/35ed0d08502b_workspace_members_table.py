"""workspace_members table, backfilled

Revision ID: 35ed0d08502b
Revises: 15981123afd0
Create Date: 2026-08-25 09:00:00.000000

The backfill (every existing user -> member of the oldest workspace, at their
current global role) runs in the same revision as the table it populates,
rather than as a separate migration: a data-only revision would add no column
or table of its own, and `db/migrate.py::REVISION_MARKERS` requires every
revision to carry at least one marker no earlier revision has
(`test_every_revision_has_at_least_one_marker_no_earlier_revision_has`) so
that a legacy `create_all` database can always be classified by inspection.
Idempotent (`ON CONFLICT DO NOTHING`, scoped to users with no membership yet)
so it is safe on both a fresh install (no users/workspaces exist yet — this is
a no-op; `services/bootstrap.py` carries the same backfill as an
application-level safety net for that path and for any user created later)
and a real upgrade of an existing single-workspace deployment.

The backfill alone leaves every upgraded database with **zero** `owner`
members: pre-tenancy global roles are `admin|analyst|approver`, none of
which is `owner`, so `member.role` above is copied straight from a role
vocabulary that never included it. Every owner-gated action (removing/
demoting an owner, `api/workspaces.py`) would then be a dead end with no one
able to take it. So, per backfilled workspace, we additionally promote its
oldest member who held the global `admin` role (ties broken by `id`, for a
deterministic pick when two admins share a `created_at`) to `role='owner'` —
the same "whoever created it" precedent `api/workspace.py`'s `ROLE_RANK`
docstring gives for how a workspace's owner is chosen elsewhere. A workspace
with no `admin`-role member at all is left with no owner, exactly as
before this migration — nothing worse, and nothing this migration can fix
on its own without inventing an owner from nothing.
"""
from alembic import op
import sqlalchemy as sa

revision = '35ed0d08502b'
down_revision = '15981123afd0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'workspace_members',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('role', sa.Text(), nullable=False, server_default='analyst'),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'workspace_id'),
    )
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO workspace_members (user_id, workspace_id, role, created_at)
            SELECT u.id, w.id, u.role, now()
            FROM users u
            CROSS JOIN LATERAL (
                SELECT id FROM workspaces ORDER BY created_at ASC LIMIT 1
            ) w
            WHERE NOT EXISTS (
                SELECT 1 FROM workspace_members wm WHERE wm.user_id = u.id
            )
            ON CONFLICT (user_id, workspace_id) DO NOTHING
            """
        )
    )
    # Promote each backfilled workspace's oldest global-admin member to
    # 'owner' — see the module docstring for why the plain role backfill
    # above leaves every one of them with no owner at all.
    op.get_bind().execute(
        sa.text(
            """
            UPDATE workspace_members wm
            SET role = 'owner'
            FROM (
                SELECT DISTINCT ON (wm2.workspace_id) wm2.workspace_id, wm2.user_id
                FROM workspace_members wm2
                JOIN users u ON u.id = wm2.user_id
                WHERE u.role = 'admin'
                ORDER BY wm2.workspace_id, u.created_at ASC, u.id ASC
            ) AS oldest_admin
            WHERE wm.workspace_id = oldest_admin.workspace_id
              AND wm.user_id = oldest_admin.user_id
            """
        )
    )


def downgrade() -> None:
    op.drop_table('workspace_members')
