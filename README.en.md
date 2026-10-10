# gpsd-mcp

> A **Streamable HTTP** MCP server that exposes the local `gpsd` (GPS daemon) to AI assistants, built on FastAPI and the official MCP Python SDK.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Transport](https://img.shields.io/badge/transport-streamable%20HTTP-orange)

[简体中文](README.md) · **English**

---

**gpsd-mcp** turns a GPS receiver attached to your machine into tools an AI assistant can call directly: the current fix, the satellite view, movement sampled over a time window, and an escape hatch for raw gpsd requests.

It does not open the serial port and does not parse NMEA itself — it talks to the `gpsd` you already have running, using the gpsd JSON protocol over TCP (port `2947` by default), and reshapes the replies into small dictionaries that a model can digest.

> This is a community project. It is not affiliated with or endorsed by the gpsd project or its maintainers.

## Table of contents

- [Features](#features)
- [Tools](#tools)
- [Getting started](#getting-started)
- [Connecting an MCP client](#connecting-an-mcp-client)
- [Configuration](#configuration)
- [Data conventions](#data-conventions)
- [Project layout](#project-layout)
- [Development and tests](#development-and-tests)
- [Security notes](#security-notes)
- [License](#license)

## Features

- **Five tools for everyday GPS questions**: status, position, satellites, sampling, raw commands.
- **A missing fix is data, not an error**: indoors or on a cold start, `gpsd_position` still succeeds and explains itself through `has_fix`, `mode` and `age_seconds`.
- **Clean payloads**: `gpsd_mcp/parse.py` normalises raw JSON into stable field names and units; fields gpsd did not send are omitted rather than set to `null`.
- **Explicit watch semantics**: `gpsd_stream` enables the watch, samples, then disables it again — gpsd only emits `TPV`/`SKY` while a watch is active.
- **One process, several shapes**: stateful streamable HTTP (with session ids) by default, plus `--stateless` and `--json-response`.
- **A `/health` probe**: reports process state *and* probes gpsd live, degrading to `degraded` instead of returning a 500.

## Tools

### `gpsd_status` — is gpsd reachable?

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `timeout_seconds` | number | `5.0` | Seconds to wait for gpsd (0.5 – 60) |

Returns `ok`, `endpoint`, `release`, `protocol`, `devices[]` (each with `path`, `driver`, `activated`) and a `summary`.

### `gpsd_position` — the current fix

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `timeout_seconds` | number | `8.0` | Seconds to wait for a fix (0.5 – 120) |
| `min_fix` | `"any"` \| `"2d"` \| `"3d"` | `"any"` | Required fix quality; `"3d"` waits until altitude is available too |

Returns `position`: `time_utc`, `age_seconds`, `mode`, `fix`, `has_fix`, `latitude_deg`, `longitude_deg`, `altitude_m`, `speed_mps`, `speed_kmh`, `track_deg`, `climb_mps`, `accuracy.{horizontal_m,vertical_m,speed_mps}`, plus a human-readable `summary`.

`position.has_fix == false` is a normal outcome, not an error.

### `gpsd_satellites` — the satellite view

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `timeout_seconds` | number | `8.0` | Seconds to wait for a SKY report (0.5 – 60) |
| `only_used` | boolean | `false` | List only the satellites used in the fix |

Returns `satellites_visible`, `satellites_used`, `dop.{gdop,pdop,hdop,vdop,tdop}` and `satellites[]` (`prn`, `constellation`, `elevation_deg`, `azimuth_deg`, `snr_db_hz`, `used`). Satellites are sorted with used ones first, strongest signal first. Use it to explain a missing or poor fix.

Constellation ids: `0` GPS, `1` SBAS, `2` Galileo, `3` BeiDou, `4` IMES, `5` QZSS, `6` GLONASS, `7` NavIC.

### `gpsd_stream` — sample the fix over time

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `duration_seconds` | number | `10.0` | Sampling window (0.5 – 120, clamped by `GPSD_MAX_STREAM_SECONDS`) |
| `max_samples` | integer | `60` | Stop after this many TPV samples (1 – 1000) |
| `only_fixes` | boolean | `false` | Drop samples without a 2D/3D fix |

Returns `stats`: `sample_count`, `valid_fixes`, `fix_ratio`, `first_time_utc`, `last_time_utc`, `distance_m` (accumulated haversine distance between consecutive fixes), `speed_avg_kmh`, `speed_max_kmh`, `altitude_min_m`, `altitude_max_m`, plus `samples[]` (each entry shaped like `position` from `gpsd_position`).

Progress is reported through MCP while sampling. Useful for measuring movement, or for checking that a fix is stable.

### `gpsd_command` — raw gpsd request (escape hatch)

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `command` | string | — | A gpsd query (`POLL`, `?DEVICES;`) or a JSON object `{"class": "POLL"}` |
| `timeout_seconds` | number | `3.0` | Overall collection window (0.5 – 60) |
| `max_messages` | integer | `50` | Stop after this many messages (1 – 500) |

Bare commands get their `?` prefix, upper-casing and trailing `;` filled in automatically. Collection uses a 0.75 s idle window so streaming commands do not eat the whole timeout.

Returns `sent`, `message_count`, `classes`, `messages[]` and a `summary`. Prefer the dedicated tools when they cover the case.

## Getting started

### Prerequisites

- **Python ≥ 3.11**
- A **running** `gpsd`; it listens on TCP `2947` by default and only accepts local connections out of the box
- Optional: [`uv`](https://docs.astral.sh/uv/) (recommended), or a plain `venv` + `pip`

First make sure gpsd itself is healthy:

```bash
gpspipe -w -n 5
# or ask it directly
printf '?VERSION;\n' | nc 127.0.0.1 2947
```

### Install

```bash
git clone https://github.com/expvintl/gpsd-mcp.git
cd gpsd-mcp

# Option A: uv (recommended)
uv sync --extra dev

# Option B: venv + pip
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### Run

```bash
uv run gpsd-mcp
# equivalent to
uv run python -m gpsd_mcp
```

On start it prints:

```text
gpsd-mcp 0.1.0 — MCP streamable HTTP at http://127.0.0.1:8765/mcp
  gpsd target : 127.0.0.1:2947
  transport   : session, SSE stream
  health      : http://127.0.0.1:8765/health
```

### Verify

```bash
curl -s http://127.0.0.1:8765/health
```

With a healthy gpsd:

```json
{"status":"ok","version":"0.1.0","gpsd":"127.0.0.1:2947","gpsd_release":"3.25","devices":["/dev/ttyUSB0"]}
```

When gpsd is unreachable the same shape comes back with `status` set to `degraded` and an `error` field added (the HTTP status stays 200).

## Connecting an MCP client

This server speaks **Streamable HTTP**, so client configuration uses `url` — no `command` / `args` needed.

Generic form:

```json
{
  "mcpServers": {
    "gpsd": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

VS Code (`.vscode/mcp.json`):

```json
{
  "servers": {
    "gpsd": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

Cursor, Claude Desktop and other clients that read `mcpServers` can use the generic form above.

> Clients that only speak **stdio** need a stdio ↔ HTTP bridge (for example `mcp-proxy`) wrapping the URL above; this project does not ship a stdio transport itself.

## Configuration

### Command line

| Flag | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `--host` | `MCP_HOST` | `127.0.0.1` | HTTP bind address |
| `--port` | `MCP_PORT` | `8765` | HTTP port |
| `--path` | `MCP_PATH` | `/mcp` | MCP endpoint path |
| `--gpsd-host` | `GPSD_HOST` | `127.0.0.1` | gpsd host |
| `--gpsd-port` | `GPSD_PORT` | `2947` | gpsd TCP port |
| `--stateless` | `MCP_STATELESS` | `false` | Serve session-less streamable HTTP |
| `--json-response` | `MCP_JSON_RESPONSE` | `false` | Answer POSTs with one JSON body instead of an SSE stream |
| `--log-level` | `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `--reload` | — | — | Auto-reload on code changes (development) |
| `--version` | — | — | Print the version and exit |

**Flags override the environment variables.** Boolean environment variables accept `1` / `true` / `yes` / `on` (case-insensitive).

These two are environment-only:

| Environment variable | Default | Description |
| --- | --- | --- |
| `GPSD_TIMEOUT` | `5.0` | Per-operation gpsd read/write timeout (seconds) |
| `GPSD_MAX_STREAM_SECONDS` | `120.0` | Upper bound on `gpsd_stream` duration (seconds) |

### Binding beyond localhost

```bash
gpsd -G                                      # gpsd needs -G to accept non-local connections
uv run gpsd-mcp --host 0.0.0.0 --port 8765   # expose the MCP endpoint too
```

These are two independent boundaries: `-G` on the gpsd side, `--host` on the MCP side. The MCP SDK's DNS-rebinding protection is enabled for localhost binds and disabled for wildcard binds such as `0.0.0.0` — in that case restrict access at the network layer yourself.

## Data conventions

| Item | Convention |
| --- | --- |
| Timestamps | UTC ISO-8601, millisecond precision, `Z` suffix; `age_seconds` gives the sample's age in seconds |
| Distances | metres |
| Speeds | both `speed_mps` and `speed_kmh` |
| Angles | degrees (`track_deg` is heading, 0 – 360) |
| Fix quality | `fix` ∈ `unknown` / `no fix` / `2D fix` / `3D fix`; `has_fix` is equivalent to `mode >= 2` |
| Missing fields | omitted entirely, never `null` |
| A large `age_seconds` | means a **stale sample**, not a live one |

## Project layout

```text
gpsd-mcp/
├── gpsd_mcp/
│   ├── __init__.py     # version
│   ├── __main__.py     # CLI entry point (python -m gpsd_mcp / gpsd-mcp)
│   ├── client.py       # asyncio TCP client for the gpsd JSON protocol
│   ├── parse.py        # raw gpsd messages → small stable dicts
│   ├── server.py       # the five MCP tools
│   └── app.py          # FastAPI app: /mcp + /health
├── tests/
│   └── test_gpsd_mcp.py
├── pyproject.toml
├── uv.lock
└── LICENSE
```

## Development and tests

```bash
uv run pytest                    # 127.0.0.1:2947 by default
GPSD_PORT=2948 uv run pytest     # a gpsd on another port
```

The tests talk to a **real gpsd**: they start the app, connect over streamable HTTP and call all five tools. When no gpsd is reachable the whole module is skipped rather than failing, so `skipped` on a machine without a receiver is expected.

## Security notes

- The server has **no authentication** and binds `127.0.0.1` by default. Put a reverse proxy and auth in front of it before exposing it.
- `gpsd_command` forwards arbitrary queries to gpsd — equivalent in power to reaching port `2947` on the host yourself.
- Location data is sensitive: coordinates returned by `gpsd_position` / `gpsd_stream` enter the model's context. Judge for yourself whether that is appropriate.

## License

[MIT](LICENSE)
