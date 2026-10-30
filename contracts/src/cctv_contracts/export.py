"""Write the v1 JSON Schemas: `python -m cctv_contracts.export [out_dir]`."""

import json
import sys
from pathlib import Path

from cctv_contracts.v1 import SCHEMA_MODELS

DEFAULT_OUT_DIR = Path(__file__).resolve().parents[2] / "schemas" / "v1"


def render_schemas() -> dict[str, str]:
    return {
        f"{name}.json": json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        for name, model in SCHEMA_MODELS.items()
    }


def main(out_dir: Path = DEFAULT_OUT_DIR) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in render_schemas().items():
        (out_dir / filename).write_text(content, encoding="utf-8", newline="\n")
        print(f"wrote {out_dir / filename}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT_DIR)
