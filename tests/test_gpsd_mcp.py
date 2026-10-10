"""Tests against a real gpsd — the whole module is skipped when none is reachable.

    python -m pytest
    GPSD_PORT=2948 python -m pytest     # a gpsd on another port
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncIterator

import httpx2
import pytest
import uvicorn
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from gpsd_mcp.app import create_app
from gpsd_mcp.client import GpsdClient

GPSD_HOST = os.environ.get("GPSD_HOST", "127.0.0.1")
GPSD_PORT = int(os.environ.get("GPSD_PORT", "2947"))
TOOLS = {"gpsd_status", "gpsd_position", "gpsd_satellites", "gpsd_stream", "gpsd_command"}

pytestmark = pytest.mark.anyio


@pytest.fixture
ndef anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _require_gpsd() -> None:
    try:
        with socket.create_connection((GPSD_HOST, GPSD_PORT), timeout=1):
            return
    except OSError:
        pytest.skip(f"no gpsd listening on {GPSD_HOST}:{GPSD_PORT}")


@contextlib.asynccontextmanager
async def running_server() -> AsyncIterator[str]:
    """Run the app under uvicorn on a free port; yield its base URL."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    app = create_app(GPSD_HOST, GPSD_PORT, host="127.0.0.1")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)


@contextlib.asynccontextmanager
async def _transport(url: str) -> AsyncIterator[object]:
    """MCP streamable-HTTP transport with a proxy-free HTTP stack.

    httpx2 (behind the MCP SDK) cannot parse ``::1``/``[::1]`` tokens in
    ``NO_PROXY``, hence ``trust_env=False``; everything here is on 127.0.0.1.
    """
    timeout = httpx2.Timeout(30.0, read=300.0)
    async with httpx2.AsyncClient(trust_env=False, timeout=timeout) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            yield streams


@contextlib.asynccontextmanager
async def mcp_client(url: str) -> AsyncIterator[Client]:
    async with Client(_transport(url)) as client:  # type: ignore[arg-type]
        yield client


async def test_client_reads_version_and_devices() -> None:
    async with GpsdClient(GPSD_HOST, GPSD_PORT) as client:
        version = await client.version()
        assert version["class"] == "VERSION"
        assert int(version["proto_major"]) >= 3
        assert isinstance(await client.devices(), list)


async def test_health_reports_gpsd() -> None:
    async with running_server() as base:
        async with httpx2.AsyncClient(trust_env=False, timeout=10) as http:
            body = (await http.get(f"{base}/health")).json()
    assert body["status"] == "ok"
    assert body["gpsd_release"]


async def test_tools_over_streamable_http() -> None:
    async with running_server() as base:
        async with mcp_client(f"{base}/mcp") as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == TOOLS

            status = await client.call_tool("gpsd_status", {})
            assert status.is_error is False
            assert status.structured_content["ok"] is True

            # A missing fix is data, not an error: the call must always succeed.
            position = await client.call_tool("gpsd_position", {"timeout_seconds": 8})
            assert position.is_error is False
            assert "summary" in position.structured_content

            satellites = await client.call_tool("gpsd_satellites", {"timeout_seconds": 8})
            assert satellites.is_error is False

            stream = await client.call_tool(
                "gpsd_stream", {"duration_seconds": 1, "max_samples": 5}
            )
            assert stream.is_error is False
            assert stream.structured_content["stats"]["sample_count"] >= 1
