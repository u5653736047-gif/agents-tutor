"""Export the bridge API's OpenAPI contract snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = REPOSITORY_ROOT / "backend" / "src"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "frontend" / "contracts" / "openapi.json"
GENERATED_NOTE = "AUTO-GENERATED: DO NOT EDIT MANUALLY."

if str(BACKEND_SOURCE) not in sys.path:
    sys.path.insert(0, str(BACKEND_SOURCE))

from api.app import create_app


def main() -> None:
    """Write the current OpenAPI document to the frontend contract directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    output_path = parser.parse_args().output

    openapi = create_app().openapi()
    openapi["x-generated-note"] = GENERATED_NOTE
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(openapi, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
