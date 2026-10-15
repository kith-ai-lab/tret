"""workspaces.archived_at and users.email_verified

Revision ID: a7d3e8c1f905
Revises: b6f4a2c9d3e1
Create Date: 2026-10-15 00:00:00.000000

Two columns, one revision:

* `workspaces.archived_at` — nullable timestamp, the soft delete behind
  `POST /api/workspaces/{id}/archive`. Null for every existing workspace.
* `users.email_verified` — replaces the "has a password hash" stopgap in
  `services/identity.py::account_email_is_verified`. Server default false;
  backfilled below.

Backfill: every row with a non-null `password_hash` is set true. Those are
the accounts the old heuristic already treated as verified — created by an
administrator (`POST /api/auth/users`) or the bootstrap admin, whose address
the administrator vouched for — so no one loses the pending-invite behaviour
they had. OIDC-only rows (null `password_hash`) stay false until their next
login writes the IdP's live `email_verified` claim.
"""
from alembic import op
import sqlalchemy as sa

revision = "a7d3e8c1f905"
down_revision = "b6f4a2c9d3e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(sa.text("UPDATE users SET email_verified = true WHERE password_hash IS NOT NULL"))


def downgrade() -> None:
    op.drop_column("users", "email_verified")
    op.drop_column("workspaces", "archived_at")
