"""Deployment configuration must preserve the workspace security boundary."""

from __future__ import annotations

from pathlib import Path

from api.app import DEFAULT_WORKSPACE_ROOT

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_unconfigured_workspace_defaults_to_process_working_directory() -> None:
    assert Path(DEFAULT_WORKSPACE_ROOT).resolve() == Path.cwd().resolve()


def test_stage3_script_explicitly_binds_and_cleans_workspace_root() -> None:
    script = (_REPOSITORY_ROOT / "scripts" / "start-stage3.ps1").read_text(encoding="utf-8")

    assert script.count('"API_WORKSPACE_ROOT"') >= 3
    assert "$env:API_WORKSPACE_ROOT = $repositoryRoot" in script


def test_compose_mounts_the_agent_workspace_read_only() -> None:
    compose = (_REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "API_WORKSPACE_ROOT: /workspace" in compose
    assert "- ./:/workspace:ro" in compose


def test_workspace_setting_is_documented() -> None:
    env_example = (_REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (_REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "API_WORKSPACE_ROOT" in env_example
    assert "API_WORKSPACE_ROOT" in readme
