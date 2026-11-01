from cctv_cloud.openapi import OPENAPI_PATH, REFERENCE_PATH, render, render_reference


def test_committed_openapi_is_up_to_date():
    """The web app's API types are generated from this file; re-run
    `python -m cctv_cloud.openapi` (then `npm run api:types` in web/) after API changes."""
    assert OPENAPI_PATH.exists(), "run python -m cctv_cloud.openapi"
    assert OPENAPI_PATH.read_text(encoding="utf-8") == render(), (
        "cloud/openapi.json is stale: run python -m cctv_cloud.openapi"
    )


def test_api_reference_is_up_to_date():
    """docs/api-reference.md is generated too (plan 11.5)."""
    assert REFERENCE_PATH.read_text(encoding="utf-8") == render_reference(), (
        "docs/api-reference.md is stale: run python -m cctv_cloud.openapi"
    )
