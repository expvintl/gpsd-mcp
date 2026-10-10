"""gpsd-mcp — a small streamable-HTTP MCP server for the ``gpsd`` GPS daemon.

``gpsd_mcp.client``  asyncio client for the gpsd JSON protocol
``gpsd_mcp.parse``   raw gpsd messages → small stable dicts
``gpsd_mcp.server``  the MCP tools
``gpsd_mcp.app``     FastAPI application serving the MCP streamable-HTTP endpoint
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
