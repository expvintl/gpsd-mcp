"""Command line entry point: ``python -m gpsd_mcp``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from typing import Any

import uvicorn

from . import __version__
from .app import create_app

__all__ = ["main"]

#: option name -> (environment variable, default)
ENV: dict[str, tuple[str, Any]] = {
    "gpsd_host": ("GPSD_HOST", "127.0.0.1"),
    "gpsd_port": ("GPSD_PORT", 2947),
    "host": ("MCP_HOST", "127.0.0.1"),
    "port": ("MCP_PORT", 8765),
    "path": ("MCP_PATH", "/mcp"),
    "stateless": ("MCP_STATELESS", False),
    "json_response": ("MCP_JSON_RESPONSE", False),
    "read_timeout": ("GPSD_TIMEOUT", 5.0),
    "max_stream_seconds": ("GPSD_MAX_STREAM_SECONDS", 120.0),
    "log_level": ("LOG_LEVEL", "INFO"),
}


def _read(name: str, default: Any) -> Any:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, (int, float)):
        return type(default)(raw)
    return raw


def settings_from_env() -> dict[str, Any]:
    """Resolve every option from the environment (used by the CLI and --reload)."""
    return {option: _read(name, default) for option, (name, default) in ENV.items()}


def _build_parser(env: dict[str, Any]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpsd-mcp",
        description=(
            "Streamable-HTTP MCP server exposing a gpsd GPS daemon. "
            "Flags override the GPSD_*/MCP_* environment variables."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"gpsd-mcp {__version__}")
    parser.add_argument("--host", default=env["host"], help="HTTP bind address (MCP_HOST)")
    parser.add_argument("--port", type=int, default=env["port"], help="HTTP port (MCP_PORT)")
    parser.add_argument("--path", default=env["path"], help="MCP endpoint path (MCP_PATH)")
    parser.add_argument("--gpsd-host", default=env["gpsd_host"], help="gpsd host (GPSD_HOST)")
    parser.add_argument(
        "--gpsd-port", type=int, default=env["gpsd_port"], help="gpsd TCP port (GPSD_PORT)"
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        default=env["stateless"],
        help="serve session-less streamable HTTP (no session ids)",
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        default=env["json_response"],
        help="answer POSTs with one JSON body instead of an SSE stream",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=env["log_level"],
        help="log level (LOG_LEVEL)",
    )
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    return parser


def _options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "host": args.host,
        "mcp_path": args.path,
        "stateless": args.stateless,
        "json_response": args.json_response,
    }


def app_factory() -> Any:
    """Import-string entry point for ``uvicorn --reload``."""
    env = settings_from_env()
    return create_app(
        env["gpsd_host"],
        env["gpsd_port"],
        host=env["host"],
        mcp_path=env["path"],
        stateless=env["stateless"],
        json_response=env["json_response"],
        read_timeout=env["read_timeout"],
        max_stream_seconds=env["max_stream_seconds"],
    )


def main(argv: Sequence[str] | None = None) -> int:
    env = settings_from_env()
    args = _build_parser(env).parse_args(argv)
    log_level = str(args.log_level).upper()

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    print(f"gpsd-mcp {__version__} — MCP streamable HTTP at http://{args.host}:{args.port}{args.path}")
    print(f"  gpsd target : {args.gpsd_host}:{args.gpsd_port}")
    print(
        f"  transport   : {'stateless' if args.stateless else 'session'}"
        f"{', json-response' if args.json_response else ', SSE stream'}"
    )
    print(f"  health      : http://{args.host}:{args.port}/health")
    sys.stdout.flush()

    uvicorn_options = {
        "host": args.host,
        "port": args.port,
        "log_level": log_level.lower(),
    }
    if args.reload:
        # The reloader needs an import string; hand the resolved options to the
        # child process through the environment.
        for option, (name, _default) in ENV.items():
            value = getattr(args, option, None)
            if value is None:
                value = env[option]
            os.environ[name] = str(value).lower() if isinstance(value, bool) else str(value)
        uvicorn.run("gpsd_mcp.__main__:app_factory", factory=True, reload=True, **uvicorn_options)
    else:
        app = create_app(
            args.gpsd_host,
            args.gpsd_port,
            read_timeout=env["read_timeout"],
            max_stream_seconds=env["max_stream_seconds"],
            **_options(args),
        )
        uvicorn.run(app, **uvicorn_options)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
