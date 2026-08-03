"""OpenAPI export script tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_export_script_writes_a_marked_openapi_snapshot(tmp_path: Path) -> None:
    output_path = tmp_path / "openapi.json"

    result = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "export_openapi.py"),
            "--output",
            str(output_path),
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    snapshot = json.loads(output_path.read_text(encoding="utf-8"))
    assert snapshot["x-generated-note"] == "AUTO-GENERATED: DO NOT EDIT MANUALLY."
    assert "ChatResponse" in snapshot["components"]["schemas"]
