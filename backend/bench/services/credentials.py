"""Provider credential storage: Fernet-encrypted with the app secret.

Env keys always win over DB keys (see ProviderRegistry).
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.config import get_settings
from bench.db.models import ProviderCredential


def get_fernet() -> Fernet:
    digest = hashlib.sha256(get_settings().secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


async def load_db_keys(db: AsyncSession) -> dict[str, str]:
    creds = (await db.execute(select(ProviderCredential))).scalars().all()
    f = get_fernet()
    out: dict[str, str] = {}
    for c in creds:
        try:
            out[c.provider] = f.decrypt(c.encrypted_key).decode()
        except Exception:
            continue
    return out
