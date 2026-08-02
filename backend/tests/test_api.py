"""FastAPI 应用骨架测试。"""

from __future__ import annotations

import asyncio
import importlib
import logging
import tomllib
from pathlib import Path
from types import ModuleType

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pytest import LogCaptureFixture, MonkeyPatch

from api.app import create_app
from core.graph_builder import CollaborativeAgentGraph
from core.sessions import SessionStore


def _load_app_module() -> ModuleType | None:
    try:
        return importlib.import_module("api.app")
    except ModuleNotFoundError as error:
        if error.name in {"api", "api.app"}:
            return None
        raise


def test_application_factory_is_available() -> None:
    module = _load_app_module()

    assert module is not None
    assert callable(module.create_app)


async def _get(app: FastAPI, path: str) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def _options(app: FastAPI, path: str) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.options(
            path,
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )


async def _get_with_sensitive_input(app: FastAPI) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(
            "GET",
            "/healthz?api_key=query-secret",
            content="message-body-secret",
            headers={"X-Api-Key": "header-secret"},
        )


def test_healthz_returns_ready_status() -> None:
    response = asyncio.run(_get(create_app(), "/healthz"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_cors_allows_local_frontend() -> None:
    response = asyncio.run(_options(create_app(), "/healthz"))

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_request_log_excludes_message_body_and_keys(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="api.request")

    response = asyncio.run(_get_with_sensitive_input(create_app()))

    assert response.status_code == 200
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "request_complete" in logs
    assert "message-body-secret" not in logs
    assert "query-secret" not in logs
    assert "header-secret" not in logs


def test_lifespan_builds_and_releases_shared_runtime(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DEEPSEEK_MODEL", "test-model")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key")
    monkeypatch.setenv("API_SESSION_STORE_PATH", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("API_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite3"))
    app = create_app()

    async def verify_runtime() -> None:
        async with app.router.lifespan_context(app):
            assert isinstance(getattr(app.state, "graph", None), CollaborativeAgentGraph)
            assert isinstance(getattr(app.state, "session_store", None), SessionStore)

    asyncio.run(verify_runtime())

    assert getattr(app.state, "graph", None) is None
    assert getattr(app.state, "session_store", None) is None


def test_wheel_configuration_includes_api_package() -> None:
    with (Path(__file__).parents[1] / "pyproject.toml").open("rb") as file:
        configuration = tomllib.load(file)

    packages = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]

    assert "src/api" in packages
