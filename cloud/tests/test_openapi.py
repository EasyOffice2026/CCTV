from cctv_cloud.openapi import OPENAPI_PATH, render


def test_committed_openapi_is_up_to_date():
    """The web app's API types are generated from this file; re-run
    `python -m cctv_cloud.openapi` (then `npm run api:types` in web/) after API changes."""
    assert OPENAPI_PATH.exists(), "run python -m cctv_cloud.openapi"
    assert OPENAPI_PATH.read_text(encoding="utf-8") == render(), (
        "cloud/openapi.json is stale: run python -m cctv_cloud.openapi"
    )
