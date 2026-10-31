"""Writes the API's OpenAPI document to cloud/openapi.json, which the web app generates
its TypeScript types from (`npm run api:types` in web/). A test fails when it is stale.

    python -m cctv_cloud.openapi
"""

import json
from pathlib import Path

from cctv_cloud.main import app

OPENAPI_PATH = Path(__file__).resolve().parents[1] / "openapi.json"


def render() -> str:
    return json.dumps(app.openapi(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> None:
    OPENAPI_PATH.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {OPENAPI_PATH}")


if __name__ == "__main__":
    main()
