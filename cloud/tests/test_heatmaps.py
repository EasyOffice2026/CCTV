"""Heatmaps (later phase): buckets from the edge, and the summed grid for the dashboard."""

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import login
from tests.test_edge_api import pair_device

HOUR = datetime(2026, 10, 31, 9, tzinfo=timezone.utc)


@pytest.fixture
def h(client, org):
    cache = {}

    def headers(name: str) -> dict[str, str]:
        if name not in cache:
            cache[name] = login(client, org, name)
        return cache[name]

    return headers


def bucket(org, start, cells, camera="cam_a1", columns=4, rows=2) -> dict:
    return {
        "camera_id": str(org.ids[camera]),
        "bucket_start": start.isoformat(),
        "columns": columns,
        "rows": rows,
        "cells": cells,
    }


def send(client, device, *buckets) -> dict:
    r = client.post("/v1/edge/heatmaps", json={"buckets": list(buckets)}, headers=device["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def heatmap(client, headers, org, camera="cam_a1", days=1):
    params = {
        "camera_id": str(org.ids[camera]),
        "from": HOUR.isoformat(),
        "to": (HOUR + timedelta(days=days)).isoformat(),
    }
    return client.get("/v1/heatmaps", params=params, headers=headers)


def test_heatmap_from_the_edge_to_the_dashboard(client, org, h):
    device = pair_device(client, org, h("owner_a"))
    assert heatmap(client, h("owner_a"), org).json() is None  # nothing yet
    result = send(
        client,
        device,
        bucket(org, HOUR, [[0, 100], [5, 20]]),
        bucket(org, HOUR + timedelta(hours=1), [[0, 50], [7, 3]]),
        bucket(org, HOUR, [[1, 1]], camera="cam_a2"),  # another branch's camera
    )
    assert result["accepted"] == 2
    assert result["rejected"] == [{"index": 2, "error": "unknown camera"}]
    # Sent again after a restart: replaces the hour.
    send(client, device, bucket(org, HOUR, [[0, 120], [5, 20]]))

    r = heatmap(client, h("manager_a"), org)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "camera_id": str(org.ids["cam_a1"]),
        "columns": 4,
        "rows": 2,
        "seconds": [170, 0, 0, 0, 0, 20, 0, 3],
        "hours": 2.0,
    }
    # A manager of branch a1 doesn't see branch a2; other tenants see nothing.
    assert heatmap(client, h("manager_a"), org, camera="cam_a2").status_code == 404
    assert heatmap(client, h("owner_b"), org).status_code == 404
    assert heatmap(client, h("owner_a"), org, days=100).status_code == 422


def test_a_changed_grid_uses_the_most_common_size(client, org, h):
    device = pair_device(client, org, h("owner_a"))
    send(
        client,
        device,
        bucket(org, HOUR, [[0, 10]]),
        bucket(org, HOUR + timedelta(hours=1), [[0, 10]]),
        bucket(org, HOUR + timedelta(hours=2), [[9, 10]], columns=8, rows=4),
    )
    grid = heatmap(client, h("owner_a"), org).json()
    assert (grid["columns"], grid["rows"], grid["seconds"][0], grid["hours"]) == (4, 2, 20, 2.0)


def test_heatmap_feature_is_available(client, org, h):
    features = {f["key"]: f for f in client.get("/v1/features", headers=h("owner_a")).json()}
    assert features["heatmap"]["availability"] == "pilot"
