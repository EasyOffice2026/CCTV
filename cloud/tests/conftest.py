"""Database tests need a disposable Postgres 16 database whose name contains "test".

    CCTV_TEST_MIGRATION_DATABASE_URL  owner role (resets the schema, runs migrations)
    CCTV_TEST_DATABASE_URL            restricted app role (subject to row-level security)

Without them, database tests are skipped.
"""

import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pyotp
import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

from cctv_cloud import security
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import UserRole
from cctv_cloud.db.models import Branch, Camera, Tenant, User, UserBranchAccess
from cctv_cloud.db.session import get_engine, get_sessionmaker
from cctv_cloud.main import app
from cctv_cloud.services.mailer import get_mailer
from cctv_cloud.services.notify import LocalBus, get_publisher, get_subscriber
from cctv_cloud.services.relay import LocalRelay, get_relay
from cctv_cloud.services.storage import get_storage

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


# --- API test fixtures --------------------------------------------------------------

# Fast password hashing for tests only.
security._hasher = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)

PASSWORD = "correct horse battery staple"


class FakeStorage:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []
        self.gets: list[tuple[str, str | None]] = []
        self.deleted: list[str] = []

    def presign_put(self, key: str, content_type: str, expires: int) -> str:
        self.puts.append((key, content_type))
        return f"https://storage.test/{key}?sig=put"

    def presign_get(self, key: str, expires: int, download_name: str | None = None) -> str:
        self.gets.append((key, download_name))
        return f"https://storage.test/{key}?sig=get"

    def delete(self, keys) -> None:
        self.deleted.extend(keys)

    def download(self, key: str, path) -> None:
        self.gets.append((key, None))
        path.write_bytes(f"media of {key}".encode())


class FakePublisher(LocalBus):
    """The in-process bus, recording what was published."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[tuple[str, dict]] = []

    def publish(self, channel: str, message: dict) -> None:
        self.messages.append((channel, message))
        super().publish(channel, message)


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))

    def last_token(self, to: str) -> str:
        body = next(b for t, _, b in reversed(self.sent) if t == to)
        return body.split("token=", 1)[1].split()[0]


@dataclass
class Org:
    """Two tenants. Tenant a has branches a1 and a2; tenant b has b1. Users in tenant a:
    owner, manager (a1), reviewer (a1), installer (a1); tenant b: owner. Plus a super-admin.
    Owners and the super-admin have 2FA set up with `totp[email]`."""

    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    emails: dict[str, str] = field(default_factory=dict)
    totp: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def fakes():
    storage, publisher, mailer = FakeStorage(), FakePublisher(), FakeMailer()
    relay = LocalRelay()
    app.dependency_overrides.update(
        {
            get_storage: lambda: storage,
            get_publisher: lambda: publisher,
            get_subscriber: lambda: publisher,
            get_mailer: lambda: mailer,
            get_relay: lambda: relay,
        }
    )
    yield storage, publisher, mailer
    app.dependency_overrides.clear()


@pytest.fixture
def client(owner_engine, fakes) -> Iterator[TestClient]:
    # One event loop for the whole test (the in-process relay and bus live on it).
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def org(owner_engine: Engine) -> Iterator[Org]:
    org = Org()
    password_hash = security.hash_password(PASSWORD)
    with Session(owner_engine) as s, s.begin():
        for t in ("a", "b"):
            tenant = Tenant(name=f"Tenant {t}", slug=f"tenant-{t}")
            s.add(tenant)
            s.flush()
            org.ids[t] = tenant.id
            for b in (f"{t}1", f"{t}2") if t == "a" else (f"{t}1",):
                branch = Branch(tenant_id=tenant.id, name=f"Branch {b}")
                s.add(branch)
                s.flush()
                org.ids[b] = branch.id
                camera = Camera(
                    tenant_id=tenant.id,
                    branch_id=branch.id,
                    name=f"Camera {b}",
                    role="kitchen",
                    rtsp_main="rtsp://10.0.0.5:554/Streaming/Channels/101",
                    rtsp_sub="rtsp://10.0.0.5:554/Streaming/Channels/102",
                )
                s.add(camera)
                s.flush()
                org.ids[f"cam_{b}"] = camera.id

        users = [
            ("owner_a", "a", UserRole.OWNER, None),
            ("manager_a", "a", UserRole.BRANCH_MANAGER, "a1"),
            ("reviewer_a", "a", UserRole.REVIEWER, "a1"),
            ("installer_a", "a", UserRole.INSTALLER, "a1"),
            ("owner_b", "b", UserRole.OWNER, None),
            ("admin", None, UserRole.SUPER_ADMIN, None),
        ]
        for name, t, role, branch in users:
            email = f"{name}@example.com"
            secret = security.new_totp_secret()
            user = User(
                tenant_id=org.ids[t] if t else None,
                email=email,
                full_name=name,
                role=role.value,
                password_hash=password_hash,
            )
            if role in (UserRole.OWNER, UserRole.SUPER_ADMIN):
                user.totp_secret_encrypted = security.encrypt_totp_secret(secret)
                org.totp[email] = secret
            s.add(user)
            s.flush()
            org.ids[name] = user.id
            org.emails[name] = email
            if branch:
                s.add(
                    UserBranchAccess(
                        tenant_id=user.tenant_id, user_id=user.id, branch_id=org.ids[branch]
                    )
                )
    yield org
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE tenants, users CASCADE"))


def totp_code(secret: str, offset_steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(time.time() + 30 * offset_steps)


def login(client: TestClient, org: Org, name: str, totp_offset: int = 0) -> dict[str, str]:
    """Returns the auth header for one of the org's users (doing 2FA when needed)."""
    email = org.emails[name]
    response = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    body = response.json()
    if "mfa_token" in body:
        response = client.post(
            "/v1/auth/mfa/verify",
            json={"token": body["mfa_token"], "code": totp_code(org.totp[email], totp_offset)},
        )
        assert response.status_code == 200, response.text
        body = response.json()
    return {"Authorization": f"Bearer {body['access_token']}"}
