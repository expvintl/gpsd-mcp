"""The MCP server: a handful of gpsd tools, nothing more."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from . import __version__, parse
from .client import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    GpsdConnectionError,
    GpsdError,
    GpsdTimeout,
    gpsd_session,
)

__all__ = ["INSTRUCTIONS", "build_server"]

INSTRUCTIONS = """\
This server exposes a local `gpsd` (GPS daemon) over MCP.

`gpsd_status` says whether gpsd is reachable, `gpsd_position` returns the current
fix, `gpsd_satellites` explains its quality, `gpsd_stream` samples movement over
a time window, and `gpsd_command` sends a raw gpsd request when the dedicated
tools are not enough.

Conventions: timestamps are UTC ISO-8601, distances are metres, speeds are m/s
(km/h too), angles are degrees. `fix` is `no fix` / `2D fix` / `3D fix`, and a
large `age_seconds` means a stale sample rather than a live one.
"""


def build_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    connect_timeout: float = 3.0,
    read_timeout: float = 5.0,
    max_stream_seconds: float = 120.0,
) -> MCPServer:
    """Create the MCP server for the gpsd instance at ``host:port``."""
    server = MCPServer(
        name="gpsd",
        title="gpsd (GPS daemon)",
        description="Query a local gpsd instance: fixes, satellites, streams.",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    def session():  # noqa: ANN202 — one short-lived connection per tool call
        return gpsd_session(host, port, connect_timeout=connect_timeout, read_timeout=read_timeout)

    def failed(exc: GpsdError, timeout: float) -> ToolError:
        """Translate a client failure into something the model can act on."""
        if isinstance(exc, GpsdConnectionError):
            return ToolError(
                f"cannot reach gpsd at {host}:{port}: {exc}. "
                "Check that gpsd is running and that --gpsd-host/--gpsd-port are right."
            )
        if isinstance(exc, GpsdTimeout):
            return ToolError(
                f"gpsd at {host}:{port} sent nothing useful within {timeout:g}s: {exc}. "
                "The receiver may have no fix yet; retry with a larger timeout_seconds."
            )
        return ToolError(f"gpsd error: {exc}")

    # ------------------------------------------------------------------ tools
    @server.tool(
        name="gpsd_status",
        title="gpsd status",
        description="Is gpsd reachable? Its version, protocol revision and tracked devices.",
    )
    async def gpsd_status(
        timeout_seconds: Annotated[
            float, Field(description="Seconds to wait for gpsd.", ge=0.5, le=60)
        ] = 5.0,
    ) -> dict[str, Any]:
        try:
            async with session() as client:
                version = await client.version(timeout=timeout_seconds)
                devices = await client.devices(timeout=timeout_seconds)
        except GpsdError as exc:
            raise failed(exc, timeout_seconds) from exc

        device_list = parse.summarize_devices(devices)
        release = version.get("release", "unknown")
        paths = ", ".join(str(d.get("path")) for d in device_list) or "no device"
        return {
            "ok": True,
            "endpoint": f"{host}:{port}",
            "release": release,
            "protocol": f"{version.get('proto_major')}.{version.get('proto_minor')}",
            "devices": device_list,
            "summary": f"gpsd {release} at {host}:{port}, {len(device_list)} device(s): {paths}",
        }

    @server.tool(
        name="gpsd_position",
        title="Current position",
        description=(
            "The latest TPV fix: latitude, longitude, altitude, speed, track and accuracy. "
            "Waits up to timeout_seconds for one, and with min_fix='3d' waits until altitude "
            "is available too. A missing fix is reported as data, not as an error."
        ),
    )
    async def gpsd_position(
        timeout_seconds: Annotated[
            float, Field(description="Seconds to wait for a fix.", ge=0.5, le=120)
        ] = 8.0,
        min_fix: Annotated[
            Literal["any", "2d", "3d"],
            Field(description="Required fix quality; 'any' returns the first TPV."),
        ] = "any",
    ) -> dict[str, Any]:
        required = {"any": None, "2d": 2, "3d": 3}[min_fix]
        try:
            async with session() as client:
                tpv = await client.latest("TPV", min_mode=required, timeout=timeout_seconds)
        except GpsdTimeout as exc:
            raise ToolError(
                f"no {min_fix} fix from gpsd at {host}:{port} within {timeout_seconds:g}s. "
                "The receiver may be indoors or still acquiring; gpsd_satellites shows the sky view."
            ) from exc
        except GpsdError as exc:
            raise failed(exc, timeout_seconds) from exc

        position = parse.summarize_tpv(tpv)
        if position.get("has_fix"):
            summary = (
                f"{position['fix']}: {position['latitude_deg']:.6f}, "
                f"{position['longitude_deg']:.6f}"
            )
            if position.get("altitude_m") is not None:
                summary += f" @ {position['altitude_m']:.1f} m"
            if position.get("speed_kmh"):
                summary += f", {position['speed_kmh']:.1f} km/h"
            if position.get("track_deg") is not None:
                summary += f", heading {position['track_deg']:.0f}°"
        else:
            summary = (
                f"no fix yet (mode={position.get('mode')}); gpsd sees the device but it has "
                "not resolved a position"
            )
        return {"ok": True, "position": position, "summary": summary}

    @server.tool(
        name="gpsd_satellites",
        title="Satellite view",
        description=(
            "The SKY report: visible/used satellite counts, DOP values and per-satellite "
            "elevation, azimuth and signal strength. Use it to explain a missing or poor fix."
        ),
    )
    async def gpsd_satellites(
        timeout_seconds: Annotated[
            float, Field(description="Seconds to wait for a SKY report.", ge=0.5, le=60)
        ] = 8.0,
        only_used: Annotated[bool, Field(description="Only list satellites used in the fix.")] = False,
    ) -> dict[str, Any]:
        try:
            async with session() as client:
                sky = await client.latest("SKY", timeout=timeout_seconds)
        except GpsdError as exc:
            raise failed(exc, timeout_seconds) from exc

        view = parse.summarize_sky(sky, only_used=only_used)
        dop = view.get("dop") or {}
        summary = (
            f"{view.get('satellites_used', '?')} of {view.get('satellites_visible', '?')} "
            "satellites used"
        )
        for key in ("hdop", "pdop"):
            if dop.get(key) is not None:
                summary += f", {key} {dop[key]}"
        return {"ok": True, "sky": view, "summary": summary}

    @server.tool(
        name="gpsd_stream",
        title="Sample the fix over time",
        description=(
            "Stream TPV samples for a window and return them with statistics (distance "
            "travelled, average/max speed, altitude range). Use it to measure movement or to "
            "check that a fix is stable. Reports progress while sampling."
        ),
    )
    async def gpsd_stream(
        ctx: Context,
        duration_seconds: Annotated[
            float, Field(description="How long to sample for.", ge=0.5, le=120)
        ] = 10.0,
        max_samples: Annotated[
            int, Field(description="Stop after this many TPV samples.", ge=1, le=1000)
        ] = 60,
        only_fixes: Annotated[bool, Field(description="Drop samples without a 2D/3D fix.")] = False,
    ) -> dict[str, Any]:
        duration = max(0.5, min(duration_seconds, max_stream_seconds))
        try:
            async with session() as client:
                await client.watch(enable=True, timeout=read_timeout)
                samples: list[dict[str, Any]] = []
                async for message in client.iter_messages(
                    duration=duration, max_messages=max_samples * 8
                ):
                    if message.get("class") != "TPV":
                        continue
                    sample = parse.summarize_tpv(message)
                    if only_fixes and not sample.get("has_fix"):
                        continue
                    samples.append(sample)
                    try:
                        await ctx.report_progress(
                            len(samples), max_samples, f"{len(samples)} samples"
                        )
                    except Exception:  # pragma: no cover — progress is optional
                        pass
                    if len(samples) >= max_samples:
                        break
                await client.watch(enable=False, timeout=read_timeout)
        except GpsdError as exc:
            raise failed(exc, duration) from exc

        stats = parse.summarize_stream(samples)
        summary = (
            f"collected {stats['sample_count']} sample(s) in {duration:g}s, "
            f"{stats.get('valid_fixes', 0)} with a fix"
        )
        if stats.get("distance_m") is not None:
            summary += f", {stats['distance_m']:.1f} m travelled"
        if stats.get("speed_avg_kmh") is not None:
            summary += (
                f", avg {stats['speed_avg_kmh']:.1f} km/h (max {stats['speed_max_kmh']:.1f} km/h)"
            )
        return {
            "ok": True,
            "duration_seconds": duration,
            "stats": stats,
            "samples": samples,
            "summary": summary,
        }

    @server.tool(
        name="gpsd_command",
        title="Raw gpsd command",
        description=(
            "Escape hatch: send a raw gpsd request and return the JSON messages it produced. "
            "Accepts 'DEVICES', '?DEVICES;', '?WATCH={\"enable\":true,\"json\":true};' or a JSON "
            "object like {\"class\": \"POLL\"}. Try the dedicated tools first."
        ),
    )
    async def gpsd_command(
        command: Annotated[str, Field(description="gpsd query ('?POLL;') or JSON object.")],
        timeout_seconds: Annotated[
            float, Field(description="Overall collection window.", ge=0.5, le=60)
        ] = 3.0,
        max_messages: Annotated[
            int, Field(description="Stop after this many messages.", ge=1, le=500)
        ] = 50,
    ) -> dict[str, Any]:
        line = command.strip()
        if not line:
            raise ToolError("command must not be empty")
        if not line.startswith(("{", "?")):
            line = f"?{line.upper()}"
        if not line.endswith(";"):
            line += ";"

        try:
            async with session() as client:
                await client.send_line(line)
                # Stop after a short silence: streaming commands would otherwise
                # run for the whole timeout.
                messages = [
                    message
                    async for message in client.iter_messages(
                        duration=timeout_seconds,
                        idle_timeout=min(0.75, timeout_seconds),
                        max_messages=max_messages,
                    )
                ]
        except GpsdError as exc:
            raise failed(exc, timeout_seconds) from exc

        classes = sorted({str(m.get("class")) for m in messages})
        return {
            "ok": True,
            "sent": line,
            "message_count": len(messages),
            "classes": classes,
            "messages": messages,
            "summary": f"sent {line!r}, got {len(messages)} message(s): "
            + (", ".join(classes) if classes else "none"),
        }

    return server
