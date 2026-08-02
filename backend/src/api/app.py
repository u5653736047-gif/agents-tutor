"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response

from api.openapi import install_openapi_contract
from core.graph_builder import CollaborativeAgentGraph
from core.models import DeepSeekSettings, create_deepseek_model
from core.nodes.react_agent import ChatModel
from core.persistence import open_sqlite_checkpointer
from core.sessions import SessionStore

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_API_KEY = "not-configured"
DEFAULT_SESSION_STORE_PATH = "data/api_sessions.sqlite3"
DEFAULT_CHECKPOINT_PATH = "data/api_checkpoints.sqlite3"
REQUEST_LOGGER = logging.getLogger("api.request")
RequestHandler = Callable[[Request], Awaitable[Response]]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create shared core resources for the lifetime of the API application."""
    model_settings = DeepSeekSettings(
        model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
        base_url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        api_key=os.getenv("DEEPSEEK_API_KEY", DEFAULT_API_KEY),
    )
    session_store_path = Path(
        os.getenv("API_SESSION_STORE_PATH", DEFAULT_SESSION_STORE_PATH)
    )
    checkpoint_path = Path(os.getenv("API_CHECKPOINT_PATH", DEFAULT_CHECKPOINT_PATH))

    with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
        session_store = SessionStore(session_store_path)
        try:
            app.state.graph = CollaborativeAgentGraph(
                model=cast(ChatModel, create_deepseek_model(model_settings)),
                checkpointer=checkpointer,
            )
            app.state.session_store = session_store
            yield
        finally:
            session_store.close()
            app.state.graph = None
            app.state.session_store = None


def create_app() -> FastAPI:
    """Create the API application."""
    app = FastAPI(lifespan=lifespan)
    install_openapi_contract(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def log_request(request: Request, call_next: RequestHandler) -> Response:
        started_at = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        REQUEST_LOGGER.info(
            "request_complete method=%s path=%s status=%s duration_ms=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
