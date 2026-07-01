"""
LLM 透明代理服务器

原理：
  客户端把 URL 从 https://api.xxx.com/v1/... 改成 http://127.0.0.1:8000/api.xxx.com/v1/...
  代理从路径提取 host，重建 https://host/剩余路径，原样转发请求和响应。

特点：
  - 零上游配置：host 从路径取，key 从 header 取，代理不持有任何上游凭证
  - 协议自动识别：从路径后缀判断（/v1/chat/completions / /v1/messages / /v1/responses ...）
  - 流式整合：按协议把 SSE chunks 整合成完整响应对象
  - 原样保存：每次调用存一个 JSON 文件（请求+响应+元数据），按 host 分目录

config.json（可选，本地用可以不要）：
  {
      "auth_tokens": ["sk-xxx"]   // 客户端访问代理用的 token；为空则不校验
  }
"""

import asyncio
import json
import logging
import traceback
import argparse
import uuid
import os
import aiohttp
import aiosqlite
from aiohttp import web
from datetime import datetime
from typing import Optional, Dict, Any
from utils import init_async_logger, get_async_logger, init_db_path
from raw_storage import save_raw_call, init_calls_table, extract_agent_metadata
from stream_merger import OpenAIChatMerger, AnthropicMessagesMerger


# ========== 命令行参数 ==========

def parse_args():
    parser = argparse.ArgumentParser(description="LLM透明代理")
    parser.add_argument("-p", "--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("-c", "--config", type=str, default="config.json", help="配置文件路径")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    return parser.parse_args()

args = parse_args()

logging.basicConfig(level=getattr(logging, args.log_level.upper()))
logger = logging.getLogger(__name__)
for h in logger.handlers[:]:
    logger.removeHandler(h)


def load_config(config_path: str) -> Dict[str, Any]:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {}


# ========== 协议识别 ==========

def detect_protocol_from_path(path: str) -> str:
    """从请求路径后缀识别协议"""
    if "/v1/messages" in path or "/messages" in path:
        return "anthropic-messages"
    if "/v1/responses" in path or "/responses" in path:
        return "openai-responses"
    if "/v1/embeddings" in path or "/embeddings" in path:
        return "embeddings"
    if "/v1/rerank" in path or "/rerank" in path:
        return "rerank"
    if "/v1/chat/completions" in path or "/chat/completions" in path:
        return "openai-chat"
    return "unknown"


# ========== 认证校验 ==========

def verify_auth(request: web.Request, config: dict) -> bool:
    """校验客户端认证；auth_tokens 为空则不校验"""
    auth_tokens = config.get("auth_tokens", [])
    if not auth_tokens:
        return True

    # x-api-key（Anthropic 风格）
    x_api_key = request.headers.get("x-api-key")
    if x_api_key and x_api_key in auth_tokens:
        return True

    # Authorization: Bearer（OpenAI 风格）
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1]
        if token in auth_tokens:
            return True

    return False


# ========== 代理服务器 ==========

class ProxyServer:
    def __init__(self, config_path: str, port: int = 8000):
        self.config = load_config(config_path)
        self.app = web.Application()
        self.port = port
        self.app.on_startup.append(self.init_async_resources)

    async def init_async_resources(self, app):
        await asyncio.to_thread(init_async_logger, "proxy", "proxy.log",
                                getattr(logging, args.log_level.upper()))
        self.async_logger = get_async_logger()
        await self.async_logger.info("Async logger initialized")
        await init_db_path("calls.db")
        await init_calls_table("calls.db")
        await self.async_logger.info("Database initialized")

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        await self.async_logger.info(f"Transparent proxy started on port {self.port}")
        await self.async_logger.info(
            f"   Usage: change client URL from https://api.xxx.com/v1/... "
            f"to http://127.0.0.1:{self.port}/api.xxx.com/v1/..."
        )

    # ========== 核心路由：catch-all 透明转发 ==========

    async def handle_proxy(self, request: web.Request) -> web.StreamResponse:
        """catch-all 路由：/api.xxx.com/v1/chat/completions → https://api.xxx.com/v1/chat/completions"""
        path = request.path
        method = request.method

        # 认证校验
        if not verify_auth(request, self.config):
            return web.Response(status=401, text=json.dumps({"error": "无效的认证令牌"}))

        # 从路径提取 host：/api.xxx.com/v1/... → host=api.xxx.com, rest=/v1/...
        path_stripped = path.lstrip("/")
        first_slash = path_stripped.find("/")
        if first_slash == -1:
            return web.Response(status=404, text=json.dumps({"error": "无效路径"}))
        host = path_stripped[:first_slash]
        rest_path = path_stripped[first_slash:]

        # 重建上游 URL
        upstream_url = f"https://{host}{rest_path}"
        protocol = detect_protocol_from_path(rest_path)

        # 构建上游 headers
        upstream_headers = self._build_upstream_headers(request)

        await self.async_logger.info(f"🔄 {method} {protocol}: host={host}, path={rest_path}")

        start_time = asyncio.get_event_loop().time()

        try:
            async with aiohttp.ClientSession() as session:
                timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_connect=30, sock_read=600)

                # GET 请求（如 /v1/models 模型列表）：纯透传，不保存
                if method == "GET":
                    async with session.get(upstream_url, headers=upstream_headers, timeout=timeout) as resp:
                        body = await resp.read()
                        await self.async_logger.info(f"   GET response: {resp.status}")
                        return web.Response(status=resp.status, body=body,
                                            content_type="application/json")

                # POST 请求（对话/补全/嵌入等）
                request_body = await request.read()
                if len(request_body) > 8000000:
                    return web.Response(status=413, text=json.dumps({"error": "请求体过大"}))

                # 解析请求
                try:
                    request_data = json.loads(request_body)
                except json.JSONDecodeError:
                    request_data = {}
                model_id = request_data.get("model", "unknown")
                is_stream = request_data.get("stream", False)

                raw_request_body = request_body
                agent_meta = extract_agent_metadata(request.headers)

                started_at = datetime.now()
                call_id = f"call-{started_at.strftime('%Y%m%d%H%M%S%f')}-{uuid.uuid4().hex[:8]}"

                await self.async_logger.info(
                    f"📝 {protocol}: host={host}, model={model_id}, stream={is_stream}, call_id={call_id}"
                )

                first_token_at = None

                async with session.post(upstream_url, headers=upstream_headers,
                                        data=request_body, timeout=timeout) as resp:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    await self.async_logger.info(f"Upstream response: {resp.status}, elapsed {elapsed:.2f}s")

                    # 失败：只记日志，不存文件
                    if resp.status != 200:
                        error_text = await resp.text()
                        await self.async_logger.error(f"Upstream error: {resp.status}, {error_text[:500]}")
                        return web.Response(status=resp.status, text=error_text,
                                            content_type="application/json")

                    # ========== 流式 ==========
                    if is_stream:
                        return await self._handle_stream(
                            request, resp, protocol, call_id, host, model_id,
                            raw_request_body, started_at, first_token_at, agent_meta
                        )
                    # ========== 非流式 ==========
                    else:
                        return await self._handle_non_stream(
                            resp, protocol, call_id, host, model_id,
                            raw_request_body, request, started_at, agent_meta
                        )
        except aiohttp.ClientError as e:
            await self.async_logger.error(f"Network error: {e}")
            return web.Response(status=502, text=json.dumps({"error": f"Upstream connection failed: {str(e)}"}))
        except Exception as e:
            await self.async_logger.error(f"Request handling error: {e}\n{traceback.format_exc()}")
            return web.Response(status=500, text=json.dumps({"error": f"Internal server error: {str(e)}"}))

    def _build_upstream_headers(self, request: web.Request) -> dict:
        """构建上游请求头：转发认证相关头，去掉 hop-by-hop 和代理特有头"""
        # hop-by-hop headers 不应转发
        skip_headers = {
            "host", "content-length", "transfer-encoding", "connection",
            "keep-alive", "upgrade", "proxy-authorization", "proxy-authenticate",
        }
        # agent 元数据头不转发给上游
        skip_headers.update({"x-session-id", "x-agent-id", "x-parent-call-id"})

        headers = {}
        for k, v in request.headers.items():
            if k.lower() not in skip_headers:
                headers[k] = v
        headers["Content-Type"] = "application/json"
        return headers

    async def _handle_stream(self, request: web.Request, resp, protocol: str, call_id: str,
                             host: str, model_id: str, raw_request_body: bytes,
                             started_at: datetime,
                             first_token_at: Optional[datetime], agent_meta: dict) -> web.StreamResponse:
        """处理流式响应：透传给客户端 + 整合保存"""
        response = web.StreamResponse(status=200, headers={
            "Content-Type": resp.headers.get("Content-Type", "text/event-stream"),
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        })
        await response.prepare(request)

        merger = None
        completed_response = None

        # 按协议选 merger
        if protocol == "openai-chat":
            merger = OpenAIChatMerger()
        elif protocol == "anthropic-messages":
            merger = AnthropicMessagesMerger()
        # openai-responses: 捕获 response.completed 事件

        try:
            async for line in resp.content:
                try:
                    if request.transport is None or request.transport.is_closing():
                        break
                    line_bytes = line
                    line_str = line.decode("utf-8")

                    await response.write(line_bytes)

                    if first_token_at is None and line_str.startswith("data:"):
                        first_token_at = datetime.now()

                    # 喂给 merger
                    if merger:
                        merger.feed_raw_line(line_str)
                    # Responses API: 捕获 response.completed
                    elif protocol == "openai-responses" and line_str.startswith("data:"):
                        try:
                            event_data = json.loads(line_str[5:].strip())
                            if event_data.get("type") == "response.completed":
                                completed_response = event_data.get("response")
                        except json.JSONDecodeError:
                            pass

                    # 结束判断
                    if line_str.strip() == "data: [DONE]":
                        break
                    if '"type":"message_stop"' in line_str or '"type": "message_stop"' in line_str:
                        break
                except Exception as e:
                    await self.async_logger.error(f"Stream processing error: {e}")
                    continue

            # 获取整合结果
            if merger:
                merged = merger.result()
                stop_reason = ((merged.get("choices") or [{}])[0].get("finish_reason")
                               if protocol == "openai-chat" else merged.get("stop_reason"))
            elif protocol == "openai-responses":
                merged = completed_response or {"note": "no response.completed captured"}
                stop_reason = completed_response.get("status") if completed_response else None
            else:
                merged = {"note": f"no merger for protocol {protocol}"}
                stop_reason = None

            # 保存 raw
            try:
                await save_raw_call(
                    "calls.db", call_id=call_id, protocol=protocol,
                    request_path=request.path, request_body=raw_request_body,
                    request_headers=request.headers, upstream_provider=host,
                    upstream_model=model_id, client_model_alias=model_id,
                    started_at=started_at, finished_at=datetime.now(),
                    first_token_ms=(int((first_token_at - started_at).total_seconds() * 1000)
                                    if first_token_at else None),
                    upstream_status=200, stop_reason=stop_reason,
                    response_body=merged, is_stream=True, **agent_meta,
                )
                await self.async_logger.info(f"Stream raw saved: {call_id}")
            except Exception as e:
                await self.async_logger.error(f"Failed to save stream raw: {e}")

        except Exception as e:
            await self.async_logger.error(f"Stream handling error: {e}")
        finally:
            try:
                if not response.prepared:
                    await response.prepare(request)
                await response.write_eof()
            except Exception:
                pass
        return response

    async def _handle_non_stream(self, resp, protocol: str, call_id: str,
                                 host: str, model_id: str, raw_request_body: bytes,
                                 orig_request: web.Request, started_at: datetime,
                                 agent_meta: dict) -> web.Response:
        """处理非流式响应：原样返回 + 保存"""
        response_json = await resp.json()

        if protocol == "openai-chat":
            stop_reason = ((response_json.get("choices") or [{}])[0].get("finish_reason"))
        elif protocol == "anthropic-messages":
            stop_reason = response_json.get("stop_reason")
        elif protocol == "openai-responses":
            stop_reason = response_json.get("status")
        else:
            stop_reason = None

        try:
            await save_raw_call(
                "calls.db", call_id=call_id, protocol=protocol,
                request_path=orig_request.path, request_body=raw_request_body,
                request_headers=orig_request.headers, upstream_provider=host,
                upstream_model=model_id, client_model_alias=model_id,
                started_at=started_at, finished_at=datetime.now(),
                upstream_status=200, stop_reason=stop_reason,
                response_body=response_json, is_stream=False, **agent_meta,
            )
            await self.async_logger.info(f"Non-stream raw saved: {call_id}")
        except Exception as e:
            await self.async_logger.error(f"Failed to save non-stream raw: {e}")

        return web.Response(status=200,
                            body=json.dumps(response_json, ensure_ascii=False).encode("utf-8"),
                            content_type="application/json")

    # ========== 前端 Web UI ==========

    async def handle_index(self, request: web.Request) -> web.Response:
        """前端管理界面"""
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def handle_api_calls(self, request: web.Request) -> web.Response:
        """调用列表 API：支持分页、筛选"""
        page = int(request.query.get("page", 1))
        page_size = int(request.query.get("page_size", 20))
        host = request.query.get("host", "")
        protocol = request.query.get("protocol", "")
        model = request.query.get("model", "")
        status = request.query.get("status", "")
        offset = (page - 1) * page_size

        where = []
        params = []
        if host:
            where.append("upstream_provider LIKE ?")
            params.append(f"%{host}%")
        if protocol:
            where.append("protocol = ?")
            params.append(protocol)
        if model:
            where.append("upstream_model LIKE ?")
            params.append(f"%{model}%")
        if status == "success":
            where.append("upstream_status = 200")
        elif status == "error":
            where.append("upstream_status != 200")

        where_clause = (" WHERE " + " AND ".join(where)) if where else ""

        async with aiosqlite.connect("calls.db") as conn:
            conn.row_factory = aiosqlite.Row
            # 总数
            cursor = await conn.execute(f"SELECT COUNT(*) as cnt FROM calls{where_clause}", params)
            row = await cursor.fetchone()
            total = row["cnt"]

            # 分页数据
            cursor = await conn.execute(
                f"""SELECT call_id, protocol, upstream_provider, upstream_model,
                           started_at, duration_ms, first_token_ms,
                           upstream_status, stop_reason, is_stream
                    FROM calls{where_clause}
                    ORDER BY started_at DESC
                    LIMIT ? OFFSET ?""",
                params + [page_size, offset],
            )
            rows = await cursor.fetchall()

        return web.json_response({
            "total": total,
            "page": page,
            "page_size": page_size,
            "data": [dict(r) for r in rows],
        })

    async def handle_api_call_detail(self, request: web.Request) -> web.Response:
        """调用详情 API：读取完整 JSON 文件"""
        call_id = request.match_info["call_id"]

        async with aiosqlite.connect("calls.db") as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,))
            row = await cursor.fetchone()

        if not row:
            return web.json_response({"error": "not found"}, status=404)

        # 读取 JSON 文件
        raw_path = row["raw_path"]
        call_data = None
        if raw_path and os.path.exists(raw_path):
            try:
                with open(raw_path, "r", encoding="utf-8") as f:
                    call_data = json.load(f)
            except Exception as e:
                call_data = {"error": f"读取文件失败: {e}"}

        return web.json_response({
            "meta": dict(row),
            "call": call_data,
        })

    async def handle_api_stats(self, request: web.Request) -> web.Response:
        """统计概览 API"""
        async with aiosqlite.connect("calls.db") as conn:
            conn.row_factory = aiosqlite.Row

            # 按 host 统计
            cursor = await conn.execute("""
                SELECT upstream_provider as host, COUNT(*) as count,
                       SUM(CASE WHEN upstream_status = 200 THEN 1 ELSE 0 END) as success,
                       AVG(duration_ms) as avg_duration
                FROM calls GROUP BY upstream_provider ORDER BY count DESC
            """)
            by_host = [dict(r) for r in await cursor.fetchall()]

            # 按协议统计
            cursor = await conn.execute("""
                SELECT protocol, COUNT(*) as count
                FROM calls GROUP BY protocol ORDER BY count DESC
            """)
            by_protocol = [dict(r) for r in await cursor.fetchall()]

            # 按模型统计
            cursor = await conn.execute("""
                SELECT upstream_model as model, COUNT(*) as count
                FROM calls GROUP BY upstream_model ORDER BY count DESC
            """)
            by_model = [dict(r) for r in await cursor.fetchall()]

            # 总览
            cursor = await conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN upstream_status = 200 THEN 1 ELSE 0 END) as success,
                       AVG(duration_ms) as avg_duration,
                       AVG(first_token_ms) as avg_first_token
                FROM calls
            """)
            overview = dict(await cursor.fetchone())

        return web.json_response({
            "overview": overview,
            "by_host": by_host,
            "by_protocol": by_protocol,
            "by_model": by_model,
        })


# ========== 前端 HTML ==========

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>数据采集管理</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, sans-serif; background: #f5f5f5; color: #333; }
.navbar { background: #1a1a2e; color: #fff; padding: 16px 24px; display: flex; align-items: center; gap: 24px; }
.navbar h1 { font-size: 18px; }
.navbar a { color: #aaa; text-decoration: none; cursor: pointer; }
.navbar a:hover, .navbar a.active { color: #fff; }
.lang-switch { margin-left: auto; display: flex; gap: 4px; }
.lang-switch button { background: transparent; color: #888; border: 1px solid #444; border-radius: 4px; padding: 4px 10px; cursor: pointer; font-size: 13px; }
.lang-switch button.active { color: #fff; border-color: #fff; }
.container { max-width: 1400px; margin: 20px auto; padding: 0 20px; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
.stat-card { background: #fff; border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.stat-card .label { color: #888; font-size: 13px; margin-bottom: 8px; }
.stat-card .value { font-size: 28px; font-weight: 600; }
.table-wrap { background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
table { width: 100%; border-collapse: collapse; }
th { background: #f8f9fa; padding: 12px 16px; text-align: left; font-size: 13px; color: #666; border-bottom: 1px solid #e0e0e0; }
td { padding: 12px 16px; border-bottom: 1px solid #f0f0f0; font-size: 14px; }
tr:hover { background: #f8f9fa; cursor: pointer; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; }
.badge-success { background: #d4edda; color: #155724; }
.badge-error { background: #f8d7da; color: #721c24; }
.badge-stream { background: #cce5ff; color: #004085; }
.filters { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.filters select, .filters input { padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }
.filters button { padding: 8px 16px; background: #1a1a2e; color: #fff; border: none; border-radius: 6px; cursor: pointer; }
.pagination { display: flex; justify-content: space-between; align-items: center; margin-top: 16px; }
.pagination button { padding: 6px 12px; border: 1px solid #ddd; background: #fff; border-radius: 6px; cursor: pointer; }
.pagination button:disabled { opacity: 0.5; cursor: not-allowed; }
.modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); display: none; z-index: 1000; }
.modal-overlay.show { display: flex; justify-content: center; align-items: flex-start; padding: 40px 20px; overflow-y: auto; }
.modal { background: #fff; border-radius: 8px; width: 100%; max-width: 1000px; max-height: 90vh; overflow: hidden; display: flex; flex-direction: column; }
.modal-header { padding: 16px 24px; border-bottom: 1px solid #e0e0e0; display: flex; justify-content: space-between; align-items: center; }
.modal-header h2 { font-size: 16px; }
.modal-close { font-size: 24px; cursor: pointer; color: #999; }
.modal-body { overflow-y: auto; padding: 24px; }
.detail-section { margin-bottom: 24px; }
.detail-section h3 { font-size: 14px; color: #888; margin-bottom: 8px; text-transform: uppercase; }
.detail-section pre { background: #f8f9fa; padding: 16px; border-radius: 6px; overflow-x: auto; font-size: 13px; line-height: 1.6; }
.tag { display: inline-block; background: #e9ecef; padding: 2px 8px; border-radius: 4px; font-size: 12px; margin-right: 4px; }
</style>
</head>
<body>
<div class="navbar">
  <h1 data-i18n="title">数据采集</h1>
  <a id="tab-list" class="active" onclick="switchTab('list')" data-i18n="tabList">调用列表</a>
  <a id="tab-stats" onclick="switchTab('stats')" data-i18n="tabStats">统计概览</a>
  <div class="lang-switch">
    <button id="lang-zh" onclick="setLang('zh')">中</button>
    <button id="lang-en" class="active" onclick="setLang('en')">EN</button>
  </div>
</div>

<div class="container">
  <div id="view-list">
    <div class="filters">
      <input id="f-host" placeholder="Host" onkeyup="if(event.key==='Enter')loadCalls()">
      <select id="f-protocol" onchange="loadCalls()">
        <option value="" data-i18n="allProtocols">全部协议</option>
        <option value="openai-chat">OpenAI Chat</option>
        <option value="anthropic-messages">Anthropic Messages</option>
        <option value="openai-responses">OpenAI Responses</option>
        <option value="embeddings">Embeddings</option>
        <option value="rerank">Rerank</option>
      </select>
      <input id="f-model" placeholder="Model" onkeyup="if(event.key==='Enter')loadCalls()" data-i18n-ph="modelPh">
      <select id="f-status" onchange="loadCalls()">
        <option value="" data-i18n="allStatus">全部状态</option>
        <option value="success" data-i18n="success">成功</option>
        <option value="error" data-i18n="error">失败</option>
      </select>
      <button onclick="loadCalls()" data-i18n="search">查询</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th data-i18n="colTime">时间</th><th>Host</th><th data-i18n="colProtocol">协议</th><th data-i18n="colModel">模型</th>
            <th data-i18n="colStatus">状态</th><th data-i18n="colDuration">耗时</th><th data-i18n="colFirstToken">首Token</th><th data-i18n="colStream">流式</th>
          </tr>
        </thead>
        <tbody id="calls-tbody"></tbody>
      </table>
    </div>
    <div class="pagination">
      <span id="page-info"></span>
      <div>
        <button id="btn-prev" onclick="changePage(-1)" data-i18n="prevPage">上一页</button>
        <button id="btn-next" onclick="changePage(1)" data-i18n="nextPage">下一页</button>
      </div>
    </div>
  </div>

  <div id="view-stats" style="display:none">
    <div id="stats-overview" class="stats-grid"></div>
    <h3 style="margin: 24px 0 16px; color:#666;" data-i18n="statsByHost">按 Host 统计</h3>
    <div class="table-wrap"><table><thead><tr><th>Host</th><th data-i18n="colCount">调用数</th><th data-i18n="success">成功</th><th data-i18n="colAvgDuration">平均耗时(ms)</th></tr></thead><tbody id="stats-host"></tbody></table></div>
    <h3 style="margin: 24px 0 16px; color:#666;" data-i18n="statsByProtocol">按协议统计</h3>
    <div class="table-wrap"><table><thead><tr><th data-i18n="colProtocol">协议</th><th data-i18n="colCount">调用数</th></tr></thead><tbody id="stats-protocol"></tbody></table></div>
    <h3 style="margin: 24px 0 16px; color:#666;" data-i18n="statsByModel">按模型统计</h3>
    <div class="table-wrap"><table><thead><tr><th data-i18n="colModel">模型</th><th data-i18n="colCount">调用数</th></tr></thead><tbody id="stats-model"></tbody></table></div>
  </div>
</div>

<div class="modal-overlay" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-header">
      <h2 id="modal-title" data-i18n="callDetail">调用详情</h2>
      <span class="modal-close" onclick="closeModal()">&times;</span>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
const i18n = {
  zh: {
    title: '数据采集', tabList: '调用列表', tabStats: '统计概览',
    allProtocols: '全部协议', allStatus: '全部状态', success: '成功', error: '失败',
    search: '查询', modelPh: '模型',
    colTime: '时间', colProtocol: '协议', colModel: '模型', colStatus: '状态',
    colDuration: '耗时', colFirstToken: '首Token', colStream: '流式',
    prevPage: '上一页', nextPage: '下一页',
    callDetail: '调用详情', metadata: '元数据', request: '请求', response: '响应',
    headersSanitized: 'Headers (脱敏)',
    noData: '暂无数据',
    pageInfo: (p, t, total) => `第 ${p} 页 / 共 ${t} 页 (${total} 条)`,
    statsByHost: '按 Host 统计', statsByProtocol: '按协议统计', statsByModel: '按模型统计',
    colCount: '调用数', colAvgDuration: '平均耗时(ms)',
    totalCalls: '总调用数', successCalls: '成功调用', avgDuration: '平均耗时', avgFirstToken: '平均首Token',
    streamBadge: '流式',
  },
  en: {
    title: 'Data Collection', tabList: 'Calls', tabStats: 'Stats',
    allProtocols: 'All Protocols', allStatus: 'All Status', success: 'Success', error: 'Error',
    search: 'Search', modelPh: 'Model',
    colTime: 'Time', colProtocol: 'Protocol', colModel: 'Model', colStatus: 'Status',
    colDuration: 'Duration', colFirstToken: 'First Token', colStream: 'Stream',
    prevPage: 'Prev', nextPage: 'Next',
    callDetail: 'Call Detail', metadata: 'Metadata', request: 'Request', response: 'Response',
    headersSanitized: 'Headers (sanitized)',
    noData: 'No data',
    pageInfo: (p, t, total) => `Page ${p} of ${t} (${total} records)`,
    statsByHost: 'By Host', statsByProtocol: 'By Protocol', statsByModel: 'By Model',
    colCount: 'Count', colAvgDuration: 'Avg Duration (ms)',
    totalCalls: 'Total Calls', successCalls: 'Success', avgDuration: 'Avg Duration', avgFirstToken: 'Avg First Token',
    streamBadge: 'Stream',
  }
};

let lang = localStorage.getItem('lang') || 'en';

function t(key) { return i18n[lang][key]; }

function setLang(l) {
  lang = l;
  localStorage.setItem('lang', l);
  applyI18n();
  document.getElementById('lang-zh').classList.toggle('active', l === 'zh');
  document.getElementById('lang-en').classList.toggle('active', l === 'en');
  // 重新渲染动态内容
  loadCalls();
  if (document.getElementById('view-stats').style.display !== 'none') loadStats();
}

function applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    if (i18n[lang][key]) el.textContent = i18n[lang][key];
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    const key = el.getAttribute('data-i18n-ph');
    if (i18n[lang][key]) el.placeholder = i18n[lang][key];
  });
  document.documentElement.lang = lang;
}

let currentPage = 1;
let total = 0;
const pageSize = 20;

async function loadCalls() {
  const host = document.getElementById('f-host').value;
  const protocol = document.getElementById('f-protocol').value;
  const model = document.getElementById('f-model').value;
  const status = document.getElementById('f-status').value;
  const params = new URLSearchParams({page: currentPage, page_size: pageSize, host, protocol, model, status});
  const res = await fetch('/api/calls?' + params);
  const data = await res.json();
  total = data.total;
  const tbody = document.getElementById('calls-tbody');
  tbody.innerHTML = data.data.map(r => `
    <tr onclick="showDetail('${r.call_id}')">
      <td>${fmtTime(r.started_at)}</td>
      <td>${r.upstream_provider || '-'}</td>
      <td><span class="tag">${r.protocol}</span></td>
      <td>${r.upstream_model || '-'}</td>
      <td>${r.upstream_status === 200 ? '<span class="badge badge-success">200</span>' : '<span class="badge badge-error">'+r.upstream_status+'</span>'}</td>
      <td>${r.duration_ms ? r.duration_ms+'ms' : '-'}</td>
      <td>${r.first_token_ms ? r.first_token_ms+'ms' : '-'}</td>
      <td>${r.is_stream ? '<span class="badge badge-stream">'+t('streamBadge')+'</span>' : '-'}</td>
    </tr>
  `).join('') || '<tr><td colspan="8" style="text-align:center;color:#999;padding:40px">'+t('noData')+'</td></tr>';
  document.getElementById('page-info').textContent = t('pageInfo')(currentPage, Math.ceil(total/pageSize), total);
  document.getElementById('btn-prev').disabled = currentPage <= 1;
  document.getElementById('btn-next').disabled = currentPage >= Math.ceil(total/pageSize);
}

function changePage(d) {
  currentPage += d;
  if (currentPage < 1) currentPage = 1;
  loadCalls();
}

async function showDetail(callId) {
  const res = await fetch('/api/calls/' + callId);
  const data = await res.json();
  const call = data.call || {};
  const meta = call.meta || data.meta || {};
  document.getElementById('modal-title').textContent = t('callDetail');
  let html = '';
  html += '<div class="detail-section"><h3>'+t('metadata')+'</h3><pre>' + JSON.stringify(meta, null, 2) + '</pre></div>';
  if (call.request) html += '<div class="detail-section"><h3>'+t('request')+'</h3><pre>' + JSON.stringify(call.request, null, 2) + '</pre></div>';
  if (call.response) html += '<div class="detail-section"><h3>'+t('response')+'</h3><pre>' + JSON.stringify(call.response, null, 2) + '</pre></div>';
  if (call.headers) html += '<div class="detail-section"><h3>'+t('headersSanitized')+'</h3><pre>' + JSON.stringify(call.headers, null, 2) + '</pre></div>';
  document.getElementById('modal-body').innerHTML = html;
  document.getElementById('modal').classList.add('show');
}

function closeModal() { document.getElementById('modal').classList.remove('show'); }

async function loadStats() {
  const res = await fetch('/api/stats');
  const data = await res.json();
  const ov = data.overview || {};
  document.getElementById('stats-overview').innerHTML = `
    <div class="stat-card"><div class="label">${t('totalCalls')}</div><div class="value">${ov.total||0}</div></div>
    <div class="stat-card"><div class="label">${t('successCalls')}</div><div class="value" style="color:#28a745">${ov.success||0}</div></div>
    <div class="stat-card"><div class="label">${t('avgDuration')}</div><div class="value">${Math.round(ov.avg_duration||0)}<span style="font-size:14px;color:#aaa">ms</span></div></div>
    <div class="stat-card"><div class="label">${t('avgFirstToken')}</div><div class="value">${Math.round(ov.avg_first_token||0)}<span style="font-size:14px;color:#aaa">ms</span></div></div>
  `;
  document.getElementById('stats-host').innerHTML = (data.by_host||[]).map(r =>
    `<tr><td>${r.host||'-'}</td><td>${r.count}</td><td style="color:#28a745">${r.success||0}</td><td>${Math.round(r.avg_duration||0)}</td></tr>`
  ).join('') || '<tr><td colspan="4" style="text-align:center;color:#999">'+t('noData')+'</td></tr>';
  document.getElementById('stats-protocol').innerHTML = (data.by_protocol||[]).map(r =>
    `<tr><td><span class="tag">${r.protocol}</span></td><td>${r.count}</td></tr>`
  ).join('');
  document.getElementById('stats-model').innerHTML = (data.by_model||[]).map(r =>
    `<tr><td>${r.model||'-'}</td><td>${r.count}</td></tr>`
  ).join('');
}

function switchTab(tab) {
  document.getElementById('view-list').style.display = tab === 'list' ? '' : 'none';
  document.getElementById('view-stats').style.display = tab === 'stats' ? '' : 'none';
  document.getElementById('tab-list').classList.toggle('active', tab === 'list');
  document.getElementById('tab-stats').classList.toggle('active', tab === 'stats');
  if (tab === 'stats') loadStats();
}

function fmtTime(s) {
  if (!s) return '-';
  return s.replace('T', ' ').substring(0, 19);
}

// 初始化
setLang(lang);
loadCalls();
</script>
</body>
</html>"""


# ========== 主函数 ==========

def _register_routes(server: "ProxyServer") -> None:
    """Register frontend + proxy routes on a server instance."""
    server.app.router.add_get("/", server.handle_index)
    server.app.router.add_get("/api/calls", server.handle_api_calls)
    server.app.router.add_get("/api/calls/{call_id}", server.handle_api_call_detail)
    server.app.router.add_get("/api/stats", server.handle_api_stats)
    server.app.router.add_route("*", "/{path_info:.*}", server.handle_proxy)


async def main():
    a = parse_args()
    logging.getLogger().setLevel(getattr(logging, a.log_level.upper()))
    server = ProxyServer(a.config, a.port, log_level=a.log_level)
    _register_routes(server)
    await server.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    return server


def start_proxy_in_thread(port: int = 8000, config: str = "config.json",
                         log_level: str = "INFO", on_started=None):
    """Start the proxy in a background daemon thread (for embedding in tray apps).

    Returns the started threading.Thread instance.
    """
    import threading

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server = ProxyServer(config, port, log_level=log_level)
        _register_routes(server)
        try:
            loop.run_until_complete(server.start())
            if on_started:
                on_started()
            loop.run_forever()
        except Exception as e:
            logger.error(f"Proxy thread error: {e}")

    t = threading.Thread(target=_run, name="proxy-loop", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    asyncio.run(main())
