from cctv_contracts.export import DEFAULT_OUT_DIR, render_schemas


def test_committed_schemas_are_up_to_date():
    """Fails when a model changed but `python -m cctv_contracts.export` was not re-run."""
    for filename, expected in render_schemas().items():
        path = DEFAULT_OUT_DIR / filename
        assert path.exists(), f"{path} missing: run python -m cctv_contracts.export"
        assert path.read_text(encoding="utf-8") == expected, (
            f"{filename} is stale: run python -m cctv_contracts.export"
        )
