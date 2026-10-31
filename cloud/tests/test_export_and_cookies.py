"""Event CSV export (plan 4.3) and the browser's refresh-token cookie (plan 4.1)."""

import csv
import io

from sqlalchemy import update
from sqlalchemy.orm import Session

from cctv_cloud.api.auth import REFRESH_COOKIE
from cctv_cloud.db.models import Camera
from tests.conftest import PASSWORD, login
from tests.test_events_api import add_events


def read_csv(response) -> list[dict[str, str]]:
    assert response.text.startswith("﻿")
    return list(csv.DictReader(io.StringIO(response.text.lstrip("﻿"))))


def test_export_follows_filters_and_branch_access(client, org, owner_engine):
    add_events(
        owner_engine,
        org,
        [
            ("a1", 5, "after_hours_person", "security", "new"),
            ("a1", 10, "phone_use", "staff_activity", "valid"),
            ("a2", 15, "after_hours_person", "security", "new"),
            ("b1", 1, "after_hours_person", "security", "new"),
        ],
    )
    owner = login(client, org, "owner_a")
    r = client.get("/v1/events/export", headers=owner)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = read_csv(r)
    assert [row["branch"] for row in rows] == ["Branch a1", "Branch a1", "Branch a2"]

    r = client.get("/v1/events/export", params={"feature": "security"}, headers=owner)
    assert [row["event_type"] for row in read_csv(r)] == ["after_hours_person"] * 2

    manager = login(client, org, "manager_a")  # a1 only
    assert {
        row["branch"] for row in read_csv(client.get("/v1/events/export", headers=manager))
    } == {"Branch a1"}
    reviewer = login(client, org, "reviewer_a")  # may view, not export
    assert client.get("/v1/events/export", headers=reviewer).status_code == 403

    exports = [
        a["details"]
        for a in client.get("/v1/audit-logs", headers=owner).json()
        if a["action"] == "export_events"
    ]
    assert len(exports) == 3  # owner twice, manager once; the refused one isn't an export
    assert {"rows": 2, "filters": {"feature": "security"}} in exports


def test_export_neutralises_spreadsheet_formulas(client, org, owner_engine):
    with Session(owner_engine) as s, s.begin():
        s.execute(
            update(Camera)
            .where(Camera.id == org.ids["cam_a1"])
            .values(name='=HYPERLINK("http://evil","x")')
        )
    add_events(owner_engine, org, [("a1", 5, "after_hours_person", "security", "new")])
    owner = login(client, org, "owner_a")
    [row] = read_csv(client.get("/v1/events/export", headers=owner))
    assert row["camera"].startswith("'=")


def test_browser_refresh_uses_an_http_only_cookie(client, org):
    r = client.post("/v1/auth/login", json={"email": org.emails["manager_a"], "password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert f"{REFRESH_COOKIE}=" in cookie
    assert "HttpOnly" in cookie and "Path=/v1/auth" in cookie
    assert "samesite=strict" in cookie.lower()

    refreshed = client.post("/v1/auth/refresh")  # no body: the cookie is used
    assert refreshed.status_code == 200, refreshed.text
    access = refreshed.json()["access_token"]
    assert (
        client.get("/v1/auth/me", headers={"Authorization": f"Bearer {access}"}).status_code == 200
    )

    logout = client.post("/v1/auth/logout", headers={"Authorization": f"Bearer {access}"})
    assert logout.status_code == 204
    assert "Max-Age=0" in logout.headers["set-cookie"]  # the browser drops the cookie
    client.cookies.clear()
    assert client.post("/v1/auth/refresh").status_code == 401
