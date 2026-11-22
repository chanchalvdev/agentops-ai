from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# The app talks to Postgres through two drivers (asyncpg for SQLAlchemy, psycopg 3 for the
# LangGraph checkpointer), and they spell TLS options differently. Hosted Postgres (Neon,
# Supabase...) hands out libpq-style URLs such as
#   postgresql://u:p@host/db?sslmode=require&channel_binding=require
# so both URLs are derived from whatever form was configured.


def _rewrite(url: str, scheme: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme=scheme, query=urlencode(params)))


def asyncpg_url(url: str) -> str:
    """SQLAlchemy+asyncpg URL: libpq `sslmode` becomes asyncpg `ssl`; `channel_binding` (libpq
    only; asyncpg negotiates SCRAM channel binding itself) is dropped."""
    params = dict(parse_qsl(urlsplit(url).query))
    if "sslmode" in params:
        params.setdefault("ssl", params.pop("sslmode"))
    params.pop("channel_binding", None)
    return _rewrite(url, "postgresql+asyncpg", params)


def psycopg_conninfo(url: str) -> str:
    """libpq/psycopg URL: asyncpg `ssl` becomes `sslmode`."""
    params = dict(parse_qsl(urlsplit(url).query))
    if "ssl" in params:
        params.setdefault("sslmode", params.pop("ssl"))
    return _rewrite(url, "postgresql", params)


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        asyncpg_url(database_url), pool_size=10, max_overflow=5, pool_pre_ping=True
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
