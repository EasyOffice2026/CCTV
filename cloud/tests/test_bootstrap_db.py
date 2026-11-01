"""bootstrap-db: the API's database role on a managed database (infra/terraform)."""

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from cctv_cloud.cli import bootstrap_db
from cctv_cloud.config import get_settings
from tests.conftest import MIGRATION_URL

ROLE = "cctv_bootstrap_test"


@pytest.fixture
def settings_for(owner_engine):
    """Points the settings at a role that doesn't exist yet; drops it afterwards."""
    settings = get_settings()
    saved = (settings.database_url, settings.migration_database_url, settings.app_db_role)
    owner = make_url(MIGRATION_URL)

    def use(password: str) -> str:
        app = owner.set(username=ROLE, password=password)
        settings.database_url = app.render_as_string(hide_password=False)
        settings.migration_database_url = MIGRATION_URL
        settings.app_db_role = ROLE
        return app.set(drivername="postgresql").render_as_string(hide_password=False)

    yield use
    settings.database_url, settings.migration_database_url, settings.app_db_role = saved
    with owner_engine.begin() as conn:
        db = make_url(MIGRATION_URL).database
        conn.execute(text(f'REVOKE CONNECT ON DATABASE "{db}" FROM {ROLE}'))
        conn.execute(text(f"DROP ROLE IF EXISTS {ROLE}"))


def test_bootstrap_creates_then_updates_the_app_role(settings_for, owner_engine):
    conninfo = settings_for("first-password-0123456789")
    assert bootstrap_db() == f"database role {ROLE} created"
    with psycopg.connect(conninfo) as conn:
        flags = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert flags == (False, False)  # row-level security applies to it

    # Run again with a new password (rotated in the secrets manager): updated.
    conninfo = settings_for("second-password-0123456789")
    assert bootstrap_db() == f"database role {ROLE} updated"
    with psycopg.connect(conninfo) as conn:
        assert conn.execute("SELECT current_user").fetchone() == (ROLE,)
