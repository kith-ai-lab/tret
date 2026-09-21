from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tret.config import get_settings

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def normalize_database_url(url: str) -> str:
    """Accept plain postgres:// URLs (Fly, Heroku-style): upgrade them to the
    asyncpg dialect and translate libpq-style sslmode params, which asyncpg
    rejects as a connect kwarg."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if "sslmode=" in url:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query))
        sslmode = params.pop("sslmode", None)
        if sslmode:
            # asyncpg accepts sslmode-style strings via its `ssl` argument;
            # 'disable' must be explicit or asyncpg attempts TLS by default.
            params["ssl"] = sslmode
        url = urlunsplit(parts._replace(query=urlencode(params)))
    return url


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        url = normalize_database_url(settings.database_url)
        kwargs: dict = {"pool_pre_ping": True}
        # Parallel delegation makes the pool load-bearing: once a run can hold
        # several children executing at once, each with its own session, the
        # pool can no longer be sized by whatever SQLAlchemy defaults to
        # implicitly. Postgres-only: sqlite (`tret run`/local mode and most
        # tests) uses NullPool/SingleThreadPool under asyncio, which reject
        # `pool_size`/`max_overflow` as unknown kwargs.
        if url.startswith("postgresql"):
            kwargs["pool_size"] = settings.db_pool_size
            kwargs["max_overflow"] = settings.db_max_overflow
        _engine = create_async_engine(url, **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with get_session_factory()() as session:
        yield session
