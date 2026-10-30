"""Database tests need a disposable Postgres 16 database whose name contains "test".

    CCTV_TEST_MIGRATION_DATABASE_URL  owner role (resets the schema, runs migrations)
    CCTV_TEST_DATABASE_URL            restricted app role (subject to row-level security)

Without them, database tests are skipped.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

from cctv_cloud.config import get_settings
from cctv_cloud.db.models import Branch, Camera, Tenant
from cctv_cloud.db.session import get_engine, get_sessionmaker

CLOUD_DIR = Path(__file__).resolve().parents[1]
MIGRATION_URL = os.environ.get("CCTV_TEST_MIGRATION_DATABASE_URL")
APP_URL = os.environ.get("CCTV_TEST_DATABASE_URL")


def reset_settings_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    config = Config(str(CLOUD_DIR / "alembic.ini"))
    config.attributes["database_url"] = MIGRATION_URL
    return config


@pytest.fixture(scope="session")
def owner_engine(alembic_config: Config) -> Iterator[Engine]:
    if not (MIGRATION_URL and APP_URL):
        pytest.skip("set CCTV_TEST_MIGRATION_DATABASE_URL and CCTV_TEST_DATABASE_URL")
    if "test" not in (make_url(MIGRATION_URL).database or ""):
        raise RuntimeError("refusing to reset a database whose name does not contain 'test'")

    engine = create_engine(MIGRATION_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config, "head")

    os.environ["CCTV_DATABASE_URL"] = APP_URL
    reset_settings_caches()
    yield engine
    engine.dispose()
    get_engine().dispose()
    reset_settings_caches()


@pytest.fixture
def app_engine(owner_engine: Engine) -> Engine:
    return get_engine()


@pytest.fixture
def tenants(owner_engine: Engine) -> Iterator[dict[str, dict[str, uuid.UUID]]]:
    """Two tenants (a, b), each with one branch and one camera, created as the owner."""
    data: dict[str, dict[str, uuid.UUID]] = {}
    with Session(owner_engine) as session, session.begin():
        for name in ("a", "b"):
            tenant = Tenant(name=f"Tenant {name}", slug=f"tenant-{name}")
            session.add(tenant)
            session.flush()
            branch = Branch(tenant_id=tenant.id, name="Main")
            session.add(branch)
            session.flush()
            camera = Camera(
                tenant_id=tenant.id,
                branch_id=branch.id,
                name="Kitchen 1",
                role="kitchen",
                rtsp_main="rtsp://192.168.1.64:554/Streaming/Channels/101",
            )
            session.add(camera)
            session.flush()
            data[name] = {"tenant": tenant.id, "branch": branch.id, "camera": camera.id}
    yield data
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE tenants CASCADE"))
