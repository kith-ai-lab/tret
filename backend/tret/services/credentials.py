"""Provider credential storage: Fernet-encrypted with the app secret.

Env keys always win over DB keys (see ProviderRegistry).
"""
from __future__ import annotations

import base64
import hashlib
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings
from tret.db.models import ProviderCredential


def get_fernet() -> Fernet:
    digest = hashlib.sha256(get_settings().secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


async def load_db_keys(
    db: AsyncSession, workspace_id: uuid.UUID | None = None
) -> dict[str, str]:
    """Decrypted provider keys stored in the DB, keyed by provider name.

    `workspace_id=None` (the default) keeps the historical behaviour: every
    stored key across every workspace, last one wins per provider. Passing a
    workspace_id scopes the query to that workspace's own credentials, for
    callers (the engine, running a specific run's harness) that must not hand
    a run one workspace's key on behalf of another.
    """
    query = select(ProviderCredential)
    if workspace_id is not None:
        query = query.where(ProviderCredential.workspace_id == workspace_id)
    creds = (await db.execute(query)).scalars().all()
    f = get_fernet()
    out: dict[str, str] = {}
    for c in creds:
        try:
            out[c.provider] = f.decrypt(c.encrypted_key).decode()
        except Exception:
            continue
    return out
