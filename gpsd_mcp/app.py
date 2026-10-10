"""FastAPI application serving the gpsd MCP server over streamable HTTP."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI

from . import __version__
from .client import GpsdError, gpsd_session
from .server import build_server

__all__ = ["create_app"]

logger = logging.getLogger(__name__)


def create_app(
    gpsd_host: str = "127.0.0.1",
    gpsd_port: int = 2947,
    *,
    host: str = "127.0.0.1",
    mcp_path: str = "/mcp",
    stateless: bool = False,
    json_response: bool = False,
    connect_timeout: float = 3.0,
    read_timeout: float = 5.0,
    max_stream_seconds: float = 120.0,
) -> FastAPI:
    """Build the app: the MCP streamable-HTTP endpoint plus a ``/health`` probe.

    ``host`` is the address the server binds to; the MCP SDK also uses it to
    decide on DNS-rebinding protection (enabled for localhost binds, left off
    for wildcard binds such as ``0.0.0.0``).
    """
    mcp = build_server(
        gpsd_host,
        gpsd_port,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        max_stream_seconds=max_stream_seconds,
    )
    mcp_asgi = mcp.streamable_http_app(
        streamable_http_path=mcp_path,
        stateless_http=stateless,
        json_response=json_response,
        host=host,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # A mounted Starlette app does not run its own lifespan, so the MCP
        # session manager is started (and stopped) here instead.
        async with mcp.session_manager.run():
            logger.info(
                "MCP endpoint ready on %s (gpsd at %s:%s)", mcp_path, gpsd_host, gpsd_port
            )
            yield

    app = FastAPI(title="gpsd MCP server", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Process health plus a live gpsd probe; never raises."""
        payload: dict[str, Any] = {
            "status": "ok",
            "version": __version__,
            "gpsd": f"{gpsd_host}:{gpsd_port}",
        }
        try:
            async with gpsd_session(
                gpsd_host,
                gpsd_port,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            ) as client:
                version = await client.version(timeout=read_timeout)
                devices = await client.devices(timeout=read_timeout)
            payload["gpsd_release"] = version.get("release")
            payload["devices"] = [d.get("path") for d in devices]
        except GpsdError as exc:
            payload["status"] = "degraded"
            payload["error"] = str(exc)
        return payload

    # Mounted last so FastAPI's own routes win; the MCP app keeps its own path,
    # which avoids Starlette's Mount redirecting /mcp to /mcp/.
    app.mount("/", mcp_asgi)
    return app
