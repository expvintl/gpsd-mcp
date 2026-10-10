"""Minimal asyncio client for the gpsd JSON protocol (TCP, default port 2947).

A client sends ``?COMMAND;`` queries or JSON objects such as
``{"class": "WATCH", "enable": true, "json": true}`` and reads back newline
terminated JSON messages carrying a ``class`` field (``VERSION``, ``TPV``,
``SKY``, ``ERROR``, ...).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 2947
_MAX_LINE_BYTES = 1 << 20  # 1 MiB: a sane ceiling for one gpsd message


class GpsdError(RuntimeError):
    """Base class for gpsd client failures."""


class GpsdConnectionError(GpsdError):
    """gpsd could not be reached, or the connection was lost."""


class GpsdTimeout(GpsdError):
    """No matching gpsd message arrived in time."""


class GpsdProtocolError(GpsdError):
    """gpsd reported an error, or sent something unparsable."""


class GpsdClient:
    """One connection to gpsd, usable as an async context manager."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        connect_timeout: float = 3.0,
        read_timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buffer = bytearray()

    # ------------------------------------------------------------------ lifecycle
    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> GpsdClient:
        if self.connected:
            return self
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.connect_timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise GpsdConnectionError(
                f"cannot connect to gpsd at {self.host}:{self.port}: {exc}"
            ) from exc
        self._buffer.clear()
        return self

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        self._buffer.clear()
        if writer is None:
            return
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
        except (OSError, asyncio.TimeoutError):  # pragma: no cover - defensive
            pass

    async def __aenter__(self) -> GpsdClient:
        return await self.connect()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ sending
    async def send_line(self, line: str) -> None:
        """Send one raw protocol line (a newline is appended when missing)."""
        if not self.connected or self._writer is None:
            raise GpsdConnectionError("not connected to gpsd")
        if not line.endswith("\n"):
            line += "\n"
        try:
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()
        except OSError as exc:
            raise GpsdConnectionError(f"lost connection to gpsd: {exc}") from exc

    async def send_command(self, command: str, arguments: Mapping[str, Any] | None = None) -> None:
        """Send the ``?COMMAND;`` form, optionally with JSON arguments."""
        name = command.lstrip("?").rstrip(";").upper()
        if arguments:
            await self.send_line(f"?{name}={json.dumps(arguments, separators=(',', ':'))};")
        else:
            await self.send_line(f"?{name};")

    # ------------------------------------------------------------------ receiving
    async def read_message(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Read the next JSON message.

        Non-JSON lines are skipped; ``{"class": "ERROR"}`` raises
        :class:`GpsdProtocolError`.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout

        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    logger.debug("skipping non-JSON gpsd line: %r", raw[:200])
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("class") == "ERROR":
                    raise GpsdProtocolError(str(message.get("message", "gpsd reported an error")))
                return message

            if len(self._buffer) > _MAX_LINE_BYTES:
                raise GpsdProtocolError("gpsd sent more than 1 MiB without a newline")
            if self._reader is None:
                raise GpsdConnectionError("not connected to gpsd")

            if deadline is None:
                remaining: float | None = None
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise GpsdTimeout("timed out waiting for a gpsd message")

            try:
                chunk = await asyncio.wait_for(self._reader.read(65536), remaining)
            except asyncio.TimeoutError:
                raise GpsdTimeout("timed out waiting for a gpsd message") from None
            except OSError as exc:
                raise GpsdConnectionError(f"lost connection to gpsd: {exc}") from exc
            if not chunk:
                raise GpsdConnectionError("gpsd closed the connection")
            self._buffer.extend(chunk)

    async def iter_messages(
        self,
        *,
        duration: float | None = None,
        idle_timeout: float | None = None,
        max_messages: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield messages until the duration/idle budget runs out."""
        loop = asyncio.get_running_loop()
        deadline = None if duration is None else loop.time() + duration
        count = 0
        while True:
            if max_messages is not None and count >= max_messages:
                return
            budgets: list[float] = []
            if deadline is not None:
                budgets.append(deadline - loop.time())
            if idle_timeout is not None:
                budgets.append(idle_timeout)
            remaining = min(budgets) if budgets else None
            if remaining is not None and remaining <= 0:
                return
            try:
                message = await self.read_message(timeout=remaining)
            except GpsdTimeout:
                return
            except GpsdConnectionError:
                logger.debug("gpsd connection closed while streaming")
                return
            count += 1
            yield message

    async def read_until(
        self,
        classes: str | Iterable[str],
        *,
        timeout: float | None = None,
        predicate: Any = None,
    ) -> dict[str, Any]:
        """Read messages until one matches ``classes`` (and ``predicate``)."""
        wanted = {classes} if isinstance(classes, str) else set(classes)
        budget = self.read_timeout if timeout is None else timeout
        async for message in self.iter_messages(duration=budget):
            if message.get("class") in wanted and (predicate is None or predicate(message)):
                return message
        names = ", ".join(sorted(wanted))
        raise GpsdTimeout(f"no {names} message from gpsd within {budget:g}s")

    # ------------------------------------------------------------------ operations
    async def version(self, *, timeout: float | None = None) -> dict[str, Any]:
        """gpsd's ``VERSION`` message."""
        await self.send_command("VERSION")
        return await self.read_until("VERSION", timeout=timeout)

    async def devices(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        """The devices gpsd is tracking."""
        await self.send_command("DEVICES")
        message = await self.read_until("DEVICES", timeout=timeout)
        return [d for d in message.get("devices", []) if isinstance(d, dict)]

    async def watch(
        self,
        *,
        enable: bool = True,
        timeout: float | None = None,
        device: str | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        """Turn the JSON stream on/off; returns the ``WATCH`` acknowledgement.

        ``device`` must be a device *path*: gpsd rejects a boolean there with
        "Invalid WATCH: didn't see quoted value when expecting string".
        """
        payload: dict[str, Any] = {"enable": enable, "json": True}
        if device is not None:
            payload["device"] = device
        payload.update({k: v for k, v in options.items() if v is not None})
        await self.send_command("WATCH", payload)
        return await self.read_until("WATCH", timeout=timeout)

    async def latest(
        self,
        cls: str,
        *,
        min_mode: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Start the stream and wait for the next ``cls`` message.

        gpsd only emits ``TPV``/``SKY`` while a watch is active, so this is how
        "the current fix" is fetched. ``min_mode`` filters ``TPV`` messages by
        fix quality (2 = 2D, 3 = 3D).
        """
        await self.watch(enable=True, timeout=timeout)
        predicate = None
        if min_mode is not None:
            predicate = lambda m: int(m.get("mode", 0)) >= min_mode  # noqa: E731
        return await self.read_until(cls, timeout=timeout, predicate=predicate)


@asynccontextmanager
async def gpsd_session(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    connect_timeout: float = 3.0,
    read_timeout: float = 5.0,
) -> AsyncIterator[GpsdClient]:
    """Open a connection and always close it again."""
    client = GpsdClient(host, port, connect_timeout=connect_timeout, read_timeout=read_timeout)
    await client.connect()
    try:
        yield client
    finally:
        await client.close()
