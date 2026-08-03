"""OpenAPI registration for bridge-layer contracts."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from api.schemas import contract_openapi_schemas


def install_openapi_contract(app: FastAPI) -> None:
    """Expose bridge DTOs in OpenAPI before their REST routes are added."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema

        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        components = schema.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        schemas.update(contract_openapi_schemas())
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
