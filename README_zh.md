# llm-tap

[English](README.md) | [中文](README_zh.md)

LLM 透明代理 + 数据采集系统。

把客户端的 LLM 请求 URL 从 `https://api.xxx.com` 改成 `http://127.0.0.1:12345/api.xxx.com`，代理自动透传请求/响应，同时原样保存每次完整调用，用于后续训练数据构建。

## 工作原理

```
客户端（Claude Code / Codex / CherryStudio / 任意 OpenAI 兼容应用）
  │
  │  URL: http://127.0.0.1:12345/api.xxx.com/v1/chat/completions
  │  Key: 真实的上游 API Key（不变）
  │
  ▼
┌─────────────────────────────────────────┐
│           透明代理服务器                 │
│                                         │
│  1. 从路径提取 host: api.xxx.com        │
│  2. 重建 URL: https://api.xxx.com/...   │
│  3. 原样转发 header 中的认证信息         │
│  4. 识别协议（chat/messages/responses） │
│  5. 流式响应透传 + 整合成完整响应对象     │
│  6. 保存完整调用到 JSON 文件             │
└─────────────────────────────────────────┘
  │
  ▼
真实 LLM 服务商（SiliconFlow / DeepSeek / 智谱 / Anthropic / OpenAI / 任意）
```

## 特点

- **零上游配置** — host 从路径取，key 从 header 透传，代理不持有任何上游凭证
- **协议自动识别** — 从路径后缀判断（`/v1/chat/completions` / `/v1/messages` / `/v1/responses`）
- **多服务商同时支持** — 客户端配多个服务商，各改各的 URL，代理自动处理
- **流式整合** — 把 SSE chunks 整合成完整的响应 JSON（等价于非流式响应）
- **原样保真** — 每次调用存一个 JSON 文件，请求+响应+元数据在一起，不做任何协议转换
- **按 host 分目录** — 数据天然按服务商分类

## 快速开始

### 1. 获取应用

**方式 A — 预构建发布版（推荐）**

从 [Releases 页面](https://github.com/luckfu/llm-tap/releases) 下载对应平台的安装包：

| 产物 | 平台 |
|------|------|
| `llm-tap-macos-arm64.tar.gz` | macOS Apple Silicon |
| `llm-tap-macos-x86_64.tar.gz` | macOS Intel |
| `llm-tap-windows-x86_64.zip` | Windows x64 |

解压后启动即可。macOS：`.app` 未签名，首次打开需执行一次 `xattr -cr /path/to/llm-tap.app` 解除隔离。Windows：若 SmartScreen 拦截，选择"更多信息 → 仍要运行"。

托盘应用默认在 `12345` 端口启动代理，可从托盘菜单打开 Web 界面，或通过 **Settings...** 修改端口。

**方式 B — 从源码运行**

```bash
pip install -r requirements.txt
python3 proxy_oneapi.py -p 12345
```

### 2. 配置客户端

把客户端的 API 地址从：
```
https://api.xxx.com/v1
```
改成：
```
http://127.0.0.1:12345/api.xxx.com/v1
```

API Key 填真实的上游 key，不变。

### 3. 正常使用

客户端照常使用，代理在后台自动采集数据。

## 客户端配置示例

### Claude Code（Anthropic 协议）

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:12345/api.anthropic.com
export ANTHROPIC_API_KEY=sk-ant-你的真实key
claude
```

如果用智谱的 Anthropic 兼容接口：
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:12345/open.bigmodel.cn/api/anthropic
export ANTHROPIC_API_KEY=你的智谱key
claude
```

### Codex CLI（OpenAI Responses 协议）

`~/.codex/config.toml`：
```toml
[model_providers.OpenAI]
name = "OpenAI"
base_url = "http://127.0.0.1:12345/api.openai.com/v1"
wire_api = "responses"
requires_openai_auth = true
```

### CherryStudio / 任意 OpenAI 兼容客户端

```
API 地址: http://127.0.0.1:12345/api.siliconflow.cn/v1
API Key:  你的真实 key
```

### 多服务商场景（hermes / openclaw 等）

各服务商各配各的 URL：
```
服务商1: http://127.0.0.1:12345/open.bigmodel.cn/api/coding/paas/v4
服务商2: http://127.0.0.1:12345/api.openai.com/v1
服务商3: http://127.0.0.1:12345/api.deepseek.com/v1
```

代理零配置，自动按 host 路由。

## 数据存储

### 文件结构

```
data/calls/
├── api.anthropic.com/
│   └── 2026/07/01/
│       └── call-20260701120000-abc123.json
├── open.bigmodel.cn/
│   └── 2026/07/01/
│       └── call-20260701130000-def456.json
└── api.openai.com/
    └── 2026/07/01/
        └── call-20260701140000-ghi789.json
```

按 host 分目录 + 日期分目录，每个文件是一次完整调用。

### 单个文件结构

```json
{
  "meta": {
    "call_id": "call-20260701120000-abc123",
    "protocol": "anthropic-messages",
    "upstream_provider": "api.anthropic.com",
    "upstream_model": "claude-sonnet-4-20250514",
    "started_at": "2026-07-01T12:00:00",
    "finished_at": "2026-07-01T12:00:05",
    "duration_ms": 5343,
    "first_token_ms": 4672,
    "upstream_status": 200,
    "stop_reason": "end_turn",
    "is_stream": true
  },
  "request": { ... },    // 该协议原样请求体
  "response": { ... },   // 整合后的完整响应（等价非流式）
  "headers": { ... }     // 脱敏后的 headers
}
```

**问和答在同一个文件里**，不做任何协议转换，保留各协议的原生结构。

### 协议保真

| 协议 | 路径后缀 | 响应结构 |
|------|----------|----------|
| OpenAI Chat | `/v1/chat/completions` | `{choices:[{message, finish_reason}], usage}` |
| Anthropic Messages | `/v1/messages` | `{content:[...], stop_reason, usage}` |
| OpenAI Responses | `/v1/responses` | `{output:[...], status, usage}` |

Anthropic 协议的 `thinking` block（含 `signature`）、`tool_use` block、`tool_result` block 等全部原样保留。

## Harness 训练数据导出

`export_harness_dataset.py` 可以把原始调用转换为通用的 agent harness trajectory JSONL。该格式不绑定 OpenAI，保留 message、tool call、tool result、reasoning、harness 元数据和原始片段，后续可再编译为 OpenAI、ShareGPT、ChatML、TRL、LLaMA-Factory 等训练框架格式。

先检查可导出数据：

```bash
python export_harness_dataset.py inspect --preview 3
```

导出 canonical JSONL：

```bash
python export_harness_dataset.py export --out exports/harness.jsonl
```

导出 ShareGPT JSON：

```bash
python export_harness_dataset.py export --format sharegpt --out data/sharegpt.json
```

ShareGPT 默认只输出 `id` 和 `conversations`。如果样本包含工具，导出器会在 `system` 轮次中注入 `<tools>...</tools>` 工具定义，并用 `<tool_call>` / `<tool_result>` 保留工具调用轨迹。可用 `--no-tools` 关闭工具定义注入；如需调试或溯源，可额外加 `--include-metadata`。

导出结构化工具调用 SFT JSONL（推荐用于支持 tool/function calling 的训练框架）：

```bash
python export_harness_dataset.py export --format tool_sft --out data/tool_sft.jsonl
```

`tool_sft` 每行包含顶层 `tools` 和 `messages`，其中 `messages` 保留 `assistant.tool_calls`、`role=tool`、`tool_call_id`，以及可用时的 `assistant.reasoning_content`。

默认读取桌面应用数据目录 `~/.llm-tap/calls.db`。如果要读取当前目录的开发数据库：

```bash
python export_harness_dataset.py --db calls.db inspect
```

## 项目结构

```
llm-tap/
├── proxy_oneapi.py        # 透明代理服务器
├── raw_storage.py         # 原始调用保真存储（含事件钩子）
├── export_harness_dataset.py # 通用 harness trajectory 数据导出
├── stream_merger.py       # 流式响应整合（OpenAI Chat / Anthropic Messages）
├── utils.py               # 异步日志 + 数据库初始化
├── tray_app.py            # 菜单栏 / 系统托盘应用入口
├── requirements-app.txt   # 代理 + 托盘应用依赖
└── .github/workflows/     # 发布构建（mac x86_64 / arm64、windows x86_64）
```

## 启动参数

```bash
python3 proxy_oneapi.py -p 12345 --log-level INFO
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-p, --port` | 12345 | 监听端口 |
| `--log-level` | INFO | 日志级别（DEBUG/INFO/WARNING/ERROR） |

## 桌面应用（菜单栏 / 系统托盘）

`tray_app.py` 把代理包装成 macOS 菜单栏 / Windows 系统托盘应用。显示水滴图标——空闲时为蓝青色，**每采集到一次调用就变绿并泛起柔光、右上角显示红色计数徽标约 2 秒**。托盘菜单可打开 Web 界面、修改监听端口（持久化到 `~/.llm-tap/settings.json`）、退出。

从源码运行：

```bash
pip install -r requirements-app.txt
# macOS 后端：
pip install pyobjc
# Windows 后端：
pip install pywin32

python3 tray_app.py                 # 默认端口 12345
LLM_TAP_PORT=9000 python3 tray_app.py
```

预构建发布版由 GitHub Actions 在每次打 `v*` 标签时自动发布，下载链接和启动说明见上方**快速开始**。作为桌面应用运行时，数据存放在 `~/.llm-tap/`。

## 设计原则

1. **流式整合 = 还原成等价的非流式响应** — 仍保留协议原生结构，不转换成别的格式
2. **失败调用不保存** — 上游返回非 200 只记日志，不存文件
3. **GET 请求纯透传** — 模型列表等 GET 请求不保存
4. **headers 脱敏** — `Authorization`、`x-api-key` 等敏感字段只保留长度信息

## Web 管理界面

浏览器访问 `http://127.0.0.1:12345/` 即可使用管理界面：
- 调用列表，支持筛选（host、协议、模型、状态）
- 调用详情查看（完整的请求/响应 JSON）
- 统计概览（按 host、协议、模型统计）
- 中英文切换

### 调用列表

![调用列表](docs/screenshots/overview-en.png)

### 调用详情

![调用详情](docs/screenshots/detail-en.png)

### 统计概览

![统计概览](docs/screenshots/stats-en.png)

## 常见问题

### curl 报 `Failed to connect to 127.0.0.1 port 7890`

系统配置了 HTTP 代理（Clash/V2Ray 等），curl 走了系统代理。加 `--noproxy '*'`：
```bash
curl --noproxy '*' http://127.0.0.1:12345/...
```

### Codex 报 `stream disconnected before completion`

Codex 可能走了系统代理。在启动 Codex 前设置：
```bash
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost
```

### 端口被占用

```bash
lsof -ti:12345 | xargs kill -9
```
