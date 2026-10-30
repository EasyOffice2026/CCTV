from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from cctv_cloud.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    # A bounded connect timeout keeps /readyz from hanging when the database is unreachable.
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.database_connect_timeout_seconds},
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


@contextmanager
def tenant_session(tenant_id: UUID) -> Iterator[Session]:
    """One transaction that can only see and write rows of `tenant_id`.

    Row-level-security policies read `app.tenant_id`. It is set transaction-locally,
    so a pooled connection never carries it into the next request. The block commits
    on success; don't call session.commit() inside it (a new transaction would start
    without the tenant set and see nothing).
    """
    with get_sessionmaker()() as session, session.begin():
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield session
