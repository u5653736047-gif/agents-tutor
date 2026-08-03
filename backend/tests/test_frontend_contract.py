"""Frontend contract generation checks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
FRONTEND_ROOT = REPOSITORY_ROOT / "frontend"
NPM_COMMAND = "npm.cmd" if sys.platform == "win32" else "npm"


def test_generated_contract_types_pass_strict_typecheck() -> None:
    result = subprocess.run(
        [NPM_COMMAND, "run", "typecheck"],
        cwd=FRONTEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    probe = FRONTEND_ROOT / "contracts" / "api.typecheck.ts"
    assert 'from "./api.generated"' in probe.read_text(encoding="utf-8")


def test_frontend_script_exports_the_openapi_snapshot() -> None:
    result = subprocess.run(
        [NPM_COMMAND, "run", "export:openapi"],
        cwd=FRONTEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    snapshot = json.loads(
        (FRONTEND_ROOT / "contracts" / "openapi.json").read_text(encoding="utf-8")
    )
    assert snapshot["x-generated-note"] == "AUTO-GENERATED: DO NOT EDIT MANUALLY."
