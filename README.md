# gpsd-mcp

> 把本机 `gpsd`（GPS 守护进程）通过 **MCP** 暴露给 AI 助手的 Streamable HTTP 服务器，基于 FastAPI 与官方 MCP Python SDK。

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Transport](https://img.shields.io/badge/transport-streamable%20HTTP-orange)

**简体中文** · [English](README.en.md)

---

**gpsd-mcp** 把一台机器上的 GPS 接收机变成 AI 助手可以直接调用的工具：当前位置、卫星视图、一段时间内的移动采样，以及一个发送原始 gpsd 指令的逃生通道。

它不读串口、也不自己解析 NMEA —— 它连接本机已经在跑的 `gpsd`，用 gpsd 的 JSON 协议取数（TCP，默认 `2947`），再把结果整理成模型容易消化的小字典。

> 本项目是社区实现，与 gpsd 项目及其维护者无隶属或背书关系。

## 目录

- [特性](#特性)
- [工具](#工具)
- [快速开始](#快速开始)
- [接入 MCP 客户端](#接入-mcp-客户端)
- [配置](#配置)
- [数据约定](#数据约定)
- [项目结构](#项目结构)
- [开发与测试](#开发与测试)
- [安全说明](#安全说明)
- [许可证](#许可证)

## 特性

- **五个工具覆盖日常 GPS 问答**：状态、定位、卫星、采样、原始指令。
- **缺失定位是数据，不是错误**：室内或冷启动时 `gpsd_position` 依然成功返回，用 `has_fix`、`mode`、`age_seconds` 说明原因。
- **输出干净**：`gpsd_mcp/parse.py` 把原始 JSON 归一化成稳定的字段名和单位；gpsd 没有的字段直接省略，不填 `null`。
- **唤醒语义明确**：`gpsd_stream` 内部先 `WATCH enable` 再采样、退出前 `WATCH disable` —— gpsd 只在 watch 激活时才推送 `TPV`/`SKY`。
- **一个进程、多种形态**：默认有状态 streamable HTTP（带 session id），也可 `--stateless` / `--json-response`。
- **`/health` 探针**：既报告进程状态，也实时探测 gpsd，探测失败返回 `degraded` 而不是 500。

## 工具

### `gpsd_status` — gpsd 是否可达

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `timeout_seconds` | number | `5.0` | 等待 gpsd 响应的秒数（0.5 – 60） |

返回 `ok`、`endpoint`、`release`、`protocol`、`devices[]`（每项含 `path`、`driver`、`activated`）与 `summary`。

### `gpsd_position` — 当前定位

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `timeout_seconds` | number | `8.0` | 等待一个定位的秒数（0.5 – 120） |
| `min_fix` | `"any"` \| `"2d"` \| `"3d"` | `"any"` | 要求的定位质量；`"3d"` 会一直等到高程可用 |

返回 `position`：`time_utc`、`age_seconds`、`mode`、`fix`、`has_fix`、`latitude_deg`、`longitude_deg`、`altitude_m`、`speed_mps`、`speed_kmh`、`track_deg`、`climb_mps`、`accuracy.{horizontal_m,vertical_m,speed_mps}`，以及一句人类可读的 `summary`。

`position.has_fix == false` 属于正常结果，不是错误。

### `gpsd_satellites` — 卫星视图

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `timeout_seconds` | number | `8.0` | 等待一份 SKY 报告的秒数（0.5 – 60） |
| `only_used` | boolean | `false` | 只列出参与定位的卫星 |

返回 `satellites_visible`、`satellites_used`、`dop.{gdop,pdop,hdop,vdop,tdop}`，以及 `satellites[]`（`prn`、`constellation`、`elevation_deg`、`azimuth_deg`、`snr_db_hz`、`used`）。卫星按「参与定位优先、信噪比从高到低」排序。用它来解释定位缺失或精度变差的原因。

星座编号映射：`0` GPS、`1` SBAS、`2` Galileo、`3` BeiDou、`4` IMES、`5` QZSS、`6` GLONASS、`7` NavIC。

### `gpsd_stream` — 一段时间内的采样

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `duration_seconds` | number | `10.0` | 采样时长（0.5 – 120，并被 `GPSD_MAX_STREAM_SECONDS` 截断） |
| `max_samples` | integer | `60` | 采满这么多 TPV 样本就提前结束（1 – 1000） |
| `only_fixes` | boolean | `false` | 丢弃没有 2D/3D 定位的样本 |

返回 `stats`：`sample_count`、`valid_fixes`、`fix_ratio`、`first_time_utc`、`last_time_utc`、`distance_m`（相邻定位点之间的 haversine 累计）、`speed_avg_kmh`、`speed_max_kmh`、`altitude_min_m`、`altitude_max_m`，以及 `samples[]`（每项的字段与 `gpsd_position` 的 `position` 相同）。

采样期间通过 MCP progress 上报进度。适合测量移动，或者确认一个定位是否稳定。

### `gpsd_command` — 原始指令（逃生通道）

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `command` | string | — | gpsd 查询（`POLL`、`?DEVICES;`）或 JSON 对象 `{"class": "POLL"}` |
| `timeout_seconds` | number | `3.0` | 整体收集窗口（0.5 – 60） |
| `max_messages` | integer | `50` | 最多收集多少条消息（1 – 500） |

裸命令会自动补齐 `?`、转成大写、补齐 `;`。收集时带 0.75 秒空闲窗口，避免流式指令占满整个超时。

返回 `sent`、`message_count`、`classes`、`messages[]` 与 `summary`。专用工具够用时请优先用专用工具。

## 快速开始

### 前置条件

- **Python ≥ 3.11**
- 一个**已经在运行**的 `gpsd`；gpsd 默认监听 TCP `2947`，且默认只接受本机连接
- 可选：[`uv`](https://docs.astral.sh/uv/)（推荐），或普通 `venv` + `pip`

先确认 gpsd 本身没问题：

```bash
gpspipe -w -n 5
# 或者直接问一句
printf '?VERSION;\n' | nc 127.0.0.1 2947
```

### 安装

```bash
git clone https://github.com/expvintl/gpsd-mcp.git
cd gpsd-mcp

# 方式 A：uv（推荐）
uv sync --extra dev

# 方式 B：venv + pip
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 运行

```bash
uv run gpsd-mcp
# 等价于
uv run python -m gpsd_mcp
```

启动后打印：

```text
gpsd-mcp 0.1.0 — MCP streamable HTTP at http://127.0.0.1:8765/mcp
  gpsd target : 127.0.0.1:2947
  transport   : session, SSE stream
  health      : http://127.0.0.1:8765/health
```

### 验证

```bash
curl -s http://127.0.0.1:8765/health
```

gpsd 正常时：

```json
{"status":"ok","version":"0.1.0","gpsd":"127.0.0.1:2947","gpsd_release":"3.25","devices":["/dev/ttyUSB0"]}
```

gpsd 不可达时返回同样的结构，但 `status` 变成 `degraded` 并带上 `error`（HTTP 状态码仍是 200）。

## 接入 MCP 客户端

本服务器说的是 **Streamable HTTP**，所以客户端配置里用 `url`，不需要 `command` / `args`。

通用写法：

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

VS Code（`.vscode/mcp.json`）：

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

Cursor / Claude Desktop 等读取 `mcpServers` 的客户端，用上面的通用写法即可。

> 只支持 **stdio** 的客户端需要一个 stdio ↔ HTTP 桥接进程（例如 `mcp-proxy`）把上面的 URL 包一层；本项目自身不提供 stdio 传输。

## 配置

### 命令行参数

| 参数 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `--host` | `MCP_HOST` | `127.0.0.1` | HTTP 绑定地址 |
| `--port` | `MCP_PORT` | `8765` | HTTP 端口 |
| `--path` | `MCP_PATH` | `/mcp` | MCP 端点路径 |
| `--gpsd-host` | `GPSD_HOST` | `127.0.0.1` | gpsd 主机 |
| `--gpsd-port` | `GPSD_PORT` | `2947` | gpsd TCP 端口 |
| `--stateless` | `MCP_STATELESS` | `false` | 提供无 session 的 streamable HTTP |
| `--json-response` | `MCP_JSON_RESPONSE` | `false` | POST 直接回一个 JSON body，而不是 SSE 流 |
| `--log-level` | `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `--reload` | — | — | 代码变更自动重载（开发用） |
| `--version` | — | — | 打印版本后退出 |

**命令行参数优先于环境变量。** 布尔型环境变量接受 `1` / `true` / `yes` / `on`（大小写不敏感）。

以下两项只能通过环境变量设置：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `GPSD_TIMEOUT` | `5.0` | 单次 gpsd 读写超时（秒） |
| `GPSD_MAX_STREAM_SECONDS` | `120.0` | `gpsd_stream` 的时长上限（秒） |

### 绑定到非本机地址

```bash
gpsd -G                                      # gpsd 需要 -G 才接受非本机连接
uv run gpsd-mcp --host 0.0.0.0 --port 8765   # 让 MCP 端点也对外
```

注意这是两条独立的安全边界：gpsd 侧的 `-G`，以及 MCP 侧的 `--host`。MCP SDK 的 DNS-rebinding 防护在绑定 localhost 时启用，绑定通配地址（如 `0.0.0.0`）时关闭 —— 此时请自己在网络层限制访问。

## 数据约定

| 项目 | 约定 |
| --- | --- |
| 时间戳 | UTC ISO-8601，毫秒精度，以 `Z` 结尾；同时给出 `age_seconds`（样本距现在的秒数） |
| 距离 | 米 |
| 速度 | 同时给出 `speed_mps` 与 `speed_kmh` |
| 角度 | 度（`track_deg` 为航向，0 – 360） |
| 定位质量 | `fix` ∈ `unknown` / `no fix` / `2D fix` / `3D fix`；`has_fix` 等价于 `mode >= 2` |
| 缺失字段 | 直接省略，不写 `null` |
| `age_seconds` 很大 | 表示这是一个**陈旧样本**，而不是实时值 |

## 项目结构

```text
gpsd-mcp/
├── gpsd_mcp/
│   ├── __init__.py     # 版本号
│   ├── __main__.py     # 命令行入口（python -m gpsd_mcp / gpsd-mcp）
│   ├── client.py       # gpsd JSON 协议的 asyncio TCP 客户端
│   ├── parse.py        # 原始 gpsd 消息 → 小而稳定的字典
│   ├── server.py       # 五个 MCP 工具的定义
│   └── app.py          # FastAPI 应用：/mcp + /health
├── tests/
│   └── test_gpsd_mcp.py
├── pyproject.toml
├── uv.lock
└── LICENSE
```

## 开发与测试

```bash
uv run pytest                    # 默认打 127.0.0.1:2947
GPSD_PORT=2948 uv run pytest     # 换一个端口
```

测试**直接打真实的 gpsd**：启动应用、走 streamable HTTP 连上去、调用全部五个工具。探测不到 gpsd 时整个模块会被 skip 而不是失败，所以在没有接收机的机器上看到 `skipped` 属于预期行为。

## 安全说明

- 服务器**没有任何鉴权**，默认只监听 `127.0.0.1`。要对外提供服务，请自行加反向代理与鉴权。
- `gpsd_command` 会把任意查询转发给 gpsd，能力等同于能直接连上本机 `2947` 端口的人。
- 定位数据本身是敏感的：`gpsd_position` / `gpsd_stream` 返回的坐标会进入模型上下文，请自行判断是否合适。

## 许可证

[MIT](LICENSE)
