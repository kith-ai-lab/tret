"""users.oidc_sub, users.session_epoch, users.disabled, backfilled

Revision ID: 8eef9d61c7c4
Revises: 3ffc3736279c
Create Date: 2026-08-25 09:10:00.000000

`disabled` is backfilled true for every user whose `password_hash` is already
NULL, in this same revision (see 35ed0d08502b's docstring for why a backfill
is not its own migration here). Pre-tenancy deactivation
(`api/auth.py::deactivate_user`) cleared the password hash outright; without
this backfill such an account would read as *active* under the new
`disabled`-column contract the moment anything (a future passwordless login
method) stops treating a null hash as itself meaning deactivated.

**WARNING — downgrading this revision silently reactivates every account
deactivated since it was applied.** Post-tenancy deactivation
(`api/auth.py::deactivate_user`) sets `disabled = true` and deliberately
*keeps* `password_hash` intact (so a later password reset can restore access
without re-provisioning the account — see `api/auth.py`'s module docstring).
`downgrade()` below drops the `disabled` column entirely; it does not touch
`password_hash`, and cannot recover which rows were disabled once the column
holding that fact is gone. Anyone paired with old application code that still
treats "password_hash IS NULL" as the deactivation signal (the pre-tenancy
contract this same column replaced) will read every one of those accounts as
active again — with no error, warning, or trace that a rollback caused it.
Do not run this downgrade against a database with any accounts deactivated
under the `disabled`-column contract unless you have independently recorded
which users they are and re-deactivate them (by whatever mechanism the
downgraded application version uses) immediately after.
"""
from alembic import op
import sqlalchemy as sa

revision = '8eef9d61c7c4'
down_revision = '3ffc3736279c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, UNIQUE: no existing user has an OIDC identity yet.
    op.add_column('users', sa.Column('oidc_sub', sa.Text(), nullable=True))
    op.create_unique_constraint('uq_users_oidc_sub', 'users', ['oidc_sub'])
    op.add_column(
        'users', sa.Column('session_epoch', sa.Integer(), nullable=False, server_default='0')
    )
    # Server default false and stays on the column: `disabled` is now the
    # deactivation signal (see api/auth.py), so a row written by code that
    # predates this column must read as active. The backfill just below
    # corrects that default for accounts already deactivated the old way.
    op.add_column(
        'users', sa.Column('disabled', sa.Boolean(), nullable=False, server_default='false')
    )
    op.get_bind().execute(
        sa.text("UPDATE users SET disabled = true WHERE password_hash IS NULL")
    )


def downgrade() -> None:
    """WARNING: drops `users.disabled` without touching `password_hash`.

    Deactivation under this revision's contract keeps `password_hash` intact
    (see this module's docstring), so any account disabled since this
    migration was applied comes back silently active the moment the column
    recording that fact is gone — no error, no log line. Confirm no account
    is currently disabled (or that you have recorded and will re-deactivate
    every one of them post-downgrade) before running this.
    """
    op.drop_column('users', 'disabled')
    op.drop_column('users', 'session_epoch')
    op.drop_constraint('uq_users_oidc_sub', 'users', type_='unique')
    op.drop_column('users', 'oidc_sub')
