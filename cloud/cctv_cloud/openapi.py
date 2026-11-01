"""Writes the API's OpenAPI document to cloud/openapi.json, which the web app generates
its TypeScript types from (`npm run api:types` in web/), and the API reference for people,
docs/api-reference.md (plan 11.5). A test fails when either is stale.

    python -m cctv_cloud.openapi
"""

import json
from pathlib import Path
from typing import Any

from cctv_cloud.main import DESCRIPTION, app

OPENAPI_PATH = Path(__file__).resolve().parents[1] / "openapi.json"
REFERENCE_PATH = Path(__file__).resolve().parents[2] / "docs" / "api-reference.md"

METHODS = ("get", "post", "put", "patch", "delete")


def render() -> str:
    return json.dumps(app.openapi(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _first_line(text: str | None) -> str:
    for line in (text or "").strip().splitlines():
        if line.strip():
            return line.strip().replace("|", "\\|")
    return ""


def _params(operation: dict[str, Any]) -> str:
    names = [
        f"`{p['name']}`" + ("" if p.get("required") else "?")
        for p in operation.get("parameters", [])
        if p.get("in") == "query"
    ]
    return ", ".join(names)


def _schema_name(content: dict[str, Any] | None) -> str:
    schema = ((content or {}).get("application/json") or {}).get("schema") or {}
    ref = schema.get("$ref") or (schema.get("items") or {}).get("$ref")
    if not ref:
        return ""
    name = ref.rsplit("/", 1)[-1]
    return f"`{name}[]`" if "items" in schema else f"`{name}`"


def render_reference() -> str:
    spec = app.openapi()
    by_tag: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for path, operations in spec["paths"].items():
        for method in METHODS:
            if method in operations:
                operation = operations[method]
                tag = (operation.get("tags") or ["other"])[0]
                by_tag.setdefault(tag, []).append((method.upper(), path, operation))
    lines = [
        "# API reference",
        "",
        "Generated from the API by `python -m cctv_cloud.openapi` (do not edit by hand; a test",
        "fails when it is stale). Every field and schema is in `cloud/openapi.json`; a running",
        "API shows it interactively at `/docs`. Edge endpoints are described with their",
        "contracts in [contracts/README.md](../contracts/README.md).",
        "",
        "## Conventions",
        DESCRIPTION.rstrip(),
        "",
        '* Errors: `{"detail": "..."}` with 401 (not signed in), 403 (not allowed), 404',
        "  (not found, also for other tenants' and branches' rows), 409 (conflict), 422",
        "  (invalid input; `detail` lists the fields), 429 (too many attempts).",
        "* Times are ISO-8601 with an offset (UTC from the API); days are branch-local dates.",
        "",
        "## Endpoints",
        "",
        "`?` marks optional query parameters.",
    ]
    for tag in sorted(by_tag):
        lines += [
            "",
            f"### {tag}",
            "",
            "| Method | Path | What | Query | Body | Returns |",
            "|---|---|---|---|---|---|",
        ]
        for method, path, operation in sorted(by_tag[tag], key=lambda x: (x[1], x[0])):
            body = _schema_name((operation.get("requestBody") or {}).get("content"))
            ok = next(
                (r for code, r in operation.get("responses", {}).items() if code.startswith("2")),
                {},
            )
            what = _first_line(operation.get("description")) or operation.get("summary", "")
            lines.append(
                f"| {method} | `{path}` | {what} | {_params(operation)} | {body} | "
                f"{_schema_name(ok.get('content'))} |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    OPENAPI_PATH.write_text(render(), encoding="utf-8", newline="\n")
    REFERENCE_PATH.write_text(render_reference(), encoding="utf-8", newline="\n")
    print(f"wrote {OPENAPI_PATH} and {REFERENCE_PATH}")


if __name__ == "__main__":
    main()
