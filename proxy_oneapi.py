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
import ipaddress
import re
import socket
import sys
import aiohttp
import aiosqlite
from aiohttp import web
from aiohttp.resolver import DefaultResolver
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from utils import init_async_logger, get_async_logger, init_db_path, ensure_ssl_cert_file
from raw_storage import RawCallWriter, init_calls_table, extract_agent_metadata
from stream_merger import OpenAIChatMerger, AnthropicMessagesMerger
from dataset_export_service import (
    ExportValidationError,
    MAX_SELECTED_CALLS,
    export_file_path,
    inspect_for_web,
    list_exports,
    run_export,
)


# ========== 命令行参数 ==========

def parse_args():
    parser = argparse.ArgumentParser(description="LLM透明代理")
    parser.add_argument("-p", "--port", type=int, default=12345, help="监听端口（默认 12345）")
    parser.add_argument("--bind", type=str, default=None,
                        help="监听地址（默认读取 config.json 的 bind，未配置时为 127.0.0.1）")
    parser.add_argument("-c", "--config", type=str, default="config.json", help="配置文件路径")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    return parser.parse_args()

logging.basicConfig(level=logging.INFO)
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


def is_error_response_body(body: Any) -> bool:
    """Return True for HTTP-200 bodies that are provider-level failures.

    Some OpenAI-compatible gateways return transport status 200 with a JSON
    body like {"code": 500, "msg": "404 NOT_FOUND", "success": false}.
    Those are useful to relay to the client but should not become training data.
    """
    if not isinstance(body, dict):
        return False

    if body.get("success") is False:
        return True

    error = body.get("error")
    if error:
        return True

    status = body.get("status")
    if status in {"failed", "incomplete", "cancelled", "canceled", "error"}:
        return True

    code = body.get("code")
    if isinstance(code, int) and code >= 400:
        return True
    if isinstance(code, str) and code.isdigit() and int(code) >= 400:
        return True

    return False


def should_persist_stream_response(protocol: str, completed_response: Any) -> bool:
    """Only persist a Responses stream after its terminal event was captured."""
    return protocol != "openai-responses" or completed_response is not None


# ========== 认证校验 ==========

def verify_auth(request: web.Request, config: dict) -> bool:
    """Validate proxy access without consuming the upstream API credential.

    proxy_tokens uses a dedicated header so Authorization/x-api-key can still
    be forwarded to the real provider. auth_tokens remains as a legacy mode.
    """
    proxy_tokens = _configured_proxy_tokens(config)
    if proxy_tokens:
        return request.headers.get("X-LLM-Tap-Token") in proxy_tokens

    auth_tokens = config.get("auth_tokens", [])
    if not auth_tokens:
        return True

    proxy_token = request.headers.get("X-LLM-Tap-Token")
    if proxy_token and proxy_token in auth_tokens:
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


def _configured_proxy_tokens(config: dict) -> list[str]:
    tokens = config.get("proxy_tokens") or []
    return [str(token) for token in tokens if str(token)]


def _configured_ui_tokens(config: dict) -> list:
    """Tokens that protect the data browser.

    ui_tokens is preferred. Falling back to auth_tokens keeps older configs
    protected if users already configured proxy-side tokens.
    """
    return list(
        config.get("ui_tokens")
        or config.get("web_tokens")
        or config.get("proxy_tokens")
        or config.get("auth_tokens")
        or []
    )


def _is_loopback_host(host: str) -> bool:
    host = (host or "").lower()
    if host.startswith("["):
        end = host.find("]")
        host = host[1:end] if end != -1 else host.strip("[]")
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_upstream_host(host: str) -> Optional[str]:
    """Return a canonical DNS name/IPv4 address, or None when unsafe."""
    candidate = (host or "").strip().rstrip(".").lower()
    if not candidate or len(candidate) > 253:
        return None
    try:
        address = ipaddress.ip_address(candidate)
        return str(address) if address.version == 4 else None
    except ValueError:
        pass
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = candidate.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        return None
    return candidate


def upstream_host_matches_allowlist(host: str, allowlist: list) -> bool:
    for item in allowlist:
        pattern = str(item).strip().rstrip(".").lower()
        if pattern.startswith("*."):
            suffix = normalize_upstream_host(pattern[2:])
            if suffix and host.endswith("." + suffix):
                return True
        elif normalize_upstream_host(pattern) == host:
            return True
    return False


def upstream_host_allowed(host: str, allowlist: list) -> bool:
    """Match public hosts by default and private addresses only when explicit."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return upstream_host_matches_allowlist(host, allowlist)
    return not allowlist or upstream_host_matches_allowlist(host, allowlist)


def _call_filter_clause(query) -> tuple[list[str], list[Any]]:
    """Build one exact, validated filter contract for list and selection APIs."""
    where: list[str] = []
    params: list[Any] = []
    host = query.get("host", "")
    protocol = query.get("protocol", "")
    model = query.get("model", "")
    status = query.get("status", "")
    start_time = query.get("start_time", "")
    end_time = query.get("end_time", "")

    if host:
        where.append("upstream_provider = ?")
        params.append(host)
    if protocol:
        where.append("protocol = ?")
        params.append(protocol)
    if model:
        where.append("upstream_model = ?")
        params.append(model)
    if status == "success":
        where.append("upstream_status = 200")
    elif status == "error":
        where.append("upstream_status != 200")

    normalized_times = []
    for value, name in ((start_time, "start_time"), (end_time, "end_time")):
        if not value:
            normalized_times.append("")
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid {name}") from exc
        if parsed.tzinfo is not None:
            raise ValueError(f"{name} must be local time without a timezone")
        normalized_times.append(parsed.isoformat())
    normalized_start, normalized_end = normalized_times
    if normalized_start and normalized_end and normalized_start > normalized_end:
        raise ValueError("start_time must not be after end_time")
    if normalized_start:
        where.append("started_at >= ?")
        params.append(normalized_start)
    if normalized_end:
        where.append("started_at <= ?")
        params.append(normalized_end)
    return where, params


class SafeUpstreamResolver(aiohttp.abc.AbstractResolver):
    """Block DNS names that unexpectedly resolve to non-public addresses."""

    def __init__(self, allowlist: list, delegate=None):
        self.allowlist = allowlist
        self.delegate = delegate or DefaultResolver()

    async def resolve(self, host: str, port: int = 0,
                      family: socket.AddressFamily = socket.AF_INET) -> list:
        results = await self.delegate.resolve(host, port, family)
        if not upstream_host_matches_allowlist(host, self.allowlist):
            for result in results:
                try:
                    address = ipaddress.ip_address(result["host"])
                except ValueError as exc:
                    raise OSError(f"invalid DNS result for upstream host {host!r}") from exc
                if not address.is_global:
                    raise OSError(f"upstream host {host!r} resolves to a non-public address")
        return results

    async def close(self) -> None:
        await self.delegate.close()


def _is_local_browser_request(request: web.Request) -> bool:
    if not _is_loopback_host(request.host):
        return False
    try:
        return ipaddress.ip_address(request.remote or "").is_loopback
    except ValueError:
        return False


def _request_ui_token(request: web.Request) -> Optional[str]:
    token = request.query.get("token")
    if token:
        return token
    token = request.cookies.get("llm_tap_ui_token")
    if token:
        return token
    token = request.headers.get("x-ui-token") or request.headers.get("x-api-key")
    if token:
        return token
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1]
    return None


def verify_ui_auth(request: web.Request, config: dict) -> bool:
    """Protect data browser pages and APIs without changing proxy forwarding."""
    tokens = _configured_ui_tokens(config)
    if not tokens:
        return _is_local_browser_request(request)
    return _request_ui_token(request) in tokens


def _maybe_set_ui_cookie(resp: web.StreamResponse, request: web.Request, config: dict) -> None:
    token = request.query.get("token")
    if token and token in _configured_ui_tokens(config):
        resp.set_cookie(
            "llm_tap_ui_token",
            token,
            max_age=30 * 24 * 3600,
            httponly=True,
            samesite="Lax",
        )


def _ui_unauthorized_json() -> web.Response:
    return web.json_response({"error": "unauthorized data browser access"}, status=401)


def _ui_unauthorized_html() -> web.Response:
    return web.Response(
        status=401,
        content_type="text/html",
        text="""<!doctype html>
<html><head><meta charset="utf-8"><title>llm-tap locked</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:720px;margin:64px auto;line-height:1.6">
<h2>llm-tap data browser is locked</h2>
<p>Remote data browsing requires <code>ui_tokens</code> in <code>config.json</code>.</p>
<p>Open <code>/?token=YOUR_TOKEN</code> once to set a browser cookie, or send <code>Authorization: Bearer YOUR_TOKEN</code> for API access.</p>
</body></html>""",
    )


# ========== 代理服务器 ==========

class ProxyServer:
    def __init__(self, config_path: str, port: int = 12345, log_level: str = "INFO",
                 bind: Optional[str] = None):
        self.ssl_cert_file = ensure_ssl_cert_file()
        self.config = load_config(config_path)
        self.bind = bind or self.config.get("bind") or "127.0.0.1"
        self.port = port
        self.log_level = log_level
        self.log_path = os.path.abspath(self.config.get("log_file", "proxy.log"))
        self.runner: Optional[web.AppRunner] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.raw_writer: Optional[RawCallWriter] = None
        self.export_lock = asyncio.Lock()
        self.db_path = "calls.db"
        self.capture_max_bytes = int(self.config.get("capture_max_bytes", 64 * 1024 * 1024))
        self.stream_capture_max_bytes = int(self.config.get("stream_capture_max_bytes", self.capture_max_bytes))
        self.request_max_bytes = int(self.config.get("request_max_bytes", 8_000_000))
        if self.request_max_bytes <= 0:
            raise ValueError("request_max_bytes must be greater than zero")
        self.upstream_allowlist = list(self.config.get("upstream_allowlist") or [])
        # 转发通道默认不设 token：上游真实凭证由客户端自带，蹭转发不会消耗本机费用。
        # 仅当绑定到非回环地址时，要求 upstream_allowlist 以避免本机被当作任意站点的开放跳板。
        # 需要限制谁能转发时，可在 config.json 额外配置 proxy_tokens（可选）。
        if not _is_loopback_host(self.bind) and not self.upstream_allowlist:
            raise ValueError("non-loopback bind requires upstream_allowlist in config.json")
        self.app = web.Application(client_max_size=self.request_max_bytes)
        self.save_queue_max = int(self.config.get("save_queue_max", 1000))
        self.save_batch_size = int(self.config.get("save_batch_size", 20))
        self.json_indent = 2 if self.config.get("pretty_json", False) else None
        self.app.on_startup.append(self.init_async_resources)

    async def init_async_resources(self, app):
        await asyncio.to_thread(init_async_logger, "proxy", self.log_path,
                                getattr(logging, self.log_level.upper()))
        self.async_logger = get_async_logger()
        await self.async_logger.info("Async logger initialized")
        if self.ssl_cert_file:
            await self.async_logger.info(f"Using CA bundle: {self.ssl_cert_file}")
        await init_db_path(self.db_path)
        await init_calls_table(self.db_path)
        self.raw_writer = RawCallWriter(
            self.db_path,
            max_queue=self.save_queue_max,
            batch_size=self.save_batch_size,
            json_indent=self.json_indent,
        )
        await self.raw_writer.start()
        await self.async_logger.info("Database initialized")
        # Reuse a single ClientSession for all upstream requests so connections
        # are pooled and DNS/TCP handshakes aren't repeated per request.
        resolver = SafeUpstreamResolver(self.upstream_allowlist)
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            limit=int(self.config.get("upstream_conn_limit", 1000)),
            limit_per_host=int(self.config.get("upstream_conn_limit_per_host", 0)),
            ttl_dns_cache=int(self.config.get("upstream_dns_cache_ttl", 300)),
            keepalive_timeout=float(self.config.get("upstream_keepalive_timeout", 30)),
            enable_cleanup_closed=True,
        )
        self.session = aiohttp.ClientSession(connector=connector)

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.bind, self.port)
        await site.start()
        await self.async_logger.info(f"Transparent proxy started on {self.bind}:{self.port}")
        await self.async_logger.info(
            f"   Usage: change client URL from https://api.xxx.com/v1/... "
            f"to http://127.0.0.1:{self.port}/api.xxx.com/v1/..."
        )

    async def stop(self):
        """Cleanly stop the AppRunner so the listening socket is released."""
        if self.session is not None:
            await self.session.close()
            self.session = None
        if self.raw_writer is not None:
            await self.raw_writer.stop(drain=True)
            self.raw_writer = None
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            await self.async_logger.info(f"Transparent proxy stopped on port {self.port}")

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
        host = normalize_upstream_host(path_stripped[:first_slash])
        if host is None:
            return web.json_response({"error": "invalid upstream host"}, status=400)
        if not upstream_host_allowed(host, self.upstream_allowlist):
            return web.json_response({"error": "upstream host is not allowed"}, status=403)
        rest_path = path_stripped[first_slash:]

        # 重建上游 URL，保留原始 query string
        query = request.rel_url.query_string
        upstream_url = f"https://{host}{rest_path}" + (f"?{query}" if query else "")
        protocol = detect_protocol_from_path(rest_path)

        # 构建上游 headers
        upstream_headers = self._build_upstream_headers(request)

        await self.async_logger.info(f"🔄 {method} {protocol}: host={host}, path={rest_path}")

        start_time = asyncio.get_event_loop().time()

        try:
            session = self.session
            if session is None:
                return web.Response(status=503, text=json.dumps({"error": "proxy session is not ready"}))
            request_timeout = float(self.config.get("upstream_timeout", 300))
            stream_read_timeout = float(self.config.get("upstream_stream_read_timeout", 600))
            normal_timeout = aiohttp.ClientTimeout(
                total=request_timeout, connect=30, sock_connect=30, sock_read=stream_read_timeout
            )

            # GET 请求（如 /v1/models 模型列表）：纯透传，不保存
            if method == "GET":
                async with session.get(upstream_url, headers=upstream_headers, timeout=normal_timeout) as resp:
                    await self.async_logger.info(f"   GET response: {resp.status}")
                    return await self._relay_response(request, resp)

            # POST 请求（对话/补全/嵌入等）
            request_body = await request.read()
            if len(request_body) > self.request_max_bytes:
                return web.Response(status=413, text=json.dumps({"error": "请求体过大"}))

            # 解析请求
            try:
                request_data = json.loads(request_body)
            except json.JSONDecodeError:
                request_data = {}
            model_id = request_data.get("model", "unknown")
            is_stream = request_data.get("stream", False)
            timeout = (
                aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=stream_read_timeout)
                if is_stream else normal_timeout
            )

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
                    error_body = await resp.read()
                    error_preview = error_body[:500].decode("utf-8", errors="replace")
                    await self.async_logger.error(f"Upstream error: {resp.status}, {error_preview}")
                    return web.Response(
                        status=resp.status,
                        body=error_body,
                        headers=self._build_response_headers(resp),
                    )

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
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "request body too large"}, status=413)
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
            "te", "trailer",
            # Avoid compressed upstream bodies so capture parsing and response
            # header passthrough remain consistent.
            "accept-encoding",
        }
        # agent 元数据头不转发给上游
        skip_headers.update({"x-session-id", "x-agent-id", "x-parent-call-id", "x-llm-tap-token"})

        headers = {}
        for k, v in request.headers.items():
            if k.lower() not in skip_headers:
                headers[k] = v
        if request.can_read_body:
            headers.setdefault("Content-Type", "application/json")
        headers["Accept-Encoding"] = "identity"
        return headers

    def _build_response_headers(self, resp) -> dict:
        skip_headers = {
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailer", "transfer-encoding", "upgrade", "content-length",
            "content-encoding",
        }
        return {k: v for k, v in resp.headers.items() if k.lower() not in skip_headers}

    async def _relay_response(self, request: web.Request, resp) -> web.StreamResponse:
        response = web.StreamResponse(status=resp.status, headers=self._build_response_headers(resp))
        await response.prepare(request)
        try:
            async for chunk in resp.content.iter_chunked(65536):
                if request.transport is None or request.transport.is_closing():
                    break
                await response.write(chunk)
        finally:
            try:
                await response.write_eof()
            except Exception:
                pass
        return response

    async def _queue_raw_save(self, *, call_id: str, **kwargs) -> None:
        if self.raw_writer is None:
            await self.async_logger.error(f"Raw save dropped for {call_id}: writer not ready")
            return
        ok = self.raw_writer.enqueue(call_id=call_id, **kwargs)
        if ok:
            await self.async_logger.info(f"Raw save queued: {call_id}")
        else:
            await self.async_logger.error(
                f"Raw save dropped for {call_id}: queue full "
                f"({self.raw_writer.max_queue}), dropped={self.raw_writer.dropped}"
            )

    async def _handle_stream(self, request: web.Request, resp, protocol: str, call_id: str,
                             host: str, model_id: str, raw_request_body: bytes,
                             started_at: datetime,
                             first_token_at: Optional[datetime], agent_meta: dict) -> web.StreamResponse:
        """处理流式响应：透传给客户端 + 整合保存"""
        headers = self._build_response_headers(resp)
        headers.setdefault("Content-Type", "text/event-stream")
        headers.setdefault("Cache-Control", "no-cache")
        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)

        merger = None
        completed_response = None
        response_event_name = None
        last_event_type = None
        captured_stream_bytes = 0
        stream_capture_truncated = False

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
                        stream_capture_truncated = True
                        break
                    line_bytes = line
                    line_str = line.decode("utf-8", errors="replace")

                    if line_str.startswith("event:"):
                        response_event_name = line_str[6:].strip() or None

                    await response.write(line_bytes)

                    if first_token_at is None and line_str.startswith("data:"):
                        first_token_at = datetime.now()

                    if (
                        self.stream_capture_max_bytes > 0
                        and captured_stream_bytes + len(line_bytes) > self.stream_capture_max_bytes
                    ):
                        stream_capture_truncated = True
                    elif not stream_capture_truncated:
                        captured_stream_bytes += len(line_bytes)
                        # 喂给 merger
                        if merger:
                            merger.feed_raw_line(line_str)
                        # Responses API: 捕获 response.completed
                        elif protocol == "openai-responses" and line_str.startswith("data:"):
                            try:
                                event_data = json.loads(line_str[5:].strip())
                                event_type = event_data.get("type") or response_event_name
                                last_event_type = event_type
                                if event_type == "response.completed":
                                    completed_response = event_data.get("response") or event_data
                            except json.JSONDecodeError:
                                pass

                    # 结束判断
                    if line_str.strip() == "data: [DONE]":
                        break
                    if '"type":"message_stop"' in line_str or '"type": "message_stop"' in line_str:
                        break
                    if not line_str.strip():
                        response_event_name = None
                except Exception as e:
                    await self.async_logger.error(f"Stream processing error: {e}")
                    continue

            # Responses API 必须以 response.completed 作为完整响应边界。
            # 缺少该事件通常意味着上游流被截断、格式不兼容或请求失败；
            # 透传已经收到的内容，但不要把占位 note 当作训练数据持久化。
            if not should_persist_stream_response(protocol, completed_response):
                await self.async_logger.warning(
                    f"Skip saving incomplete Responses stream: {call_id}, "
                    f"no response.completed captured (last_event={last_event_type!r}, "
                    f"event={response_event_name!r}, bytes={captured_stream_bytes}, "
                    f"client_closed={request.transport is None or request.transport.is_closing()})"
                )
                return response

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
            if stream_capture_truncated:
                merged["_llm_tap_truncated"] = True
                merged["_captured_bytes"] = captured_stream_bytes
                merged["_capture_max_bytes"] = self.stream_capture_max_bytes

            if is_error_response_body(merged):
                await self.async_logger.warning(
                    f"Skip saving stream error response: {call_id}, "
                    f"status={merged.get('status')!r}, code={merged.get('code')!r}, "
                    f"error={bool(merged.get('error'))}, success={merged.get('success')!r}"
                )
                return response

            # 保存 raw
            try:
                await self._queue_raw_save(
                    call_id=call_id, protocol=protocol,
                    request_path=request.raw_path, request_body=raw_request_body,
                    request_headers=list(request.headers.items()), upstream_provider=host,
                    upstream_model=model_id, client_model_alias=model_id,
                    started_at=started_at, finished_at=datetime.now(),
                    first_token_ms=(int((first_token_at - started_at).total_seconds() * 1000)
                                    if first_token_at else None),
                    upstream_status=200, stop_reason=stop_reason,
                    response_body=merged, is_stream=True, **agent_meta,
                )
                await self.async_logger.info(f"Stream raw enqueue attempted: {call_id}")
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
                                 agent_meta: dict) -> web.StreamResponse:
        """处理非流式响应：原样返回 + 保存

        边读上游边回写客户端，同时按 capture_max_bytes 累积用于后台保存。
        这样客户端不需要等待 JSON 落盘和 SQLite commit。
        """
        response = web.StreamResponse(status=200, headers=self._build_response_headers(resp))
        await response.prepare(orig_request)

        chunks = []
        captured = 0
        truncated = False
        try:
            async for chunk in resp.content.iter_chunked(65536):
                if orig_request.transport is None or orig_request.transport.is_closing():
                    truncated = True
                    break
                await response.write(chunk)

                if captured < self.capture_max_bytes:
                    remaining = self.capture_max_bytes - captured
                    chunks.append(chunk[:remaining])
                    captured += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        truncated = True
                else:
                    truncated = True
        finally:
            try:
                await response.write_eof()
            except Exception:
                pass

        raw_bytes = b"".join(chunks)
        response_json = None
        if not truncated:
            try:
                response_json = json.loads(raw_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        if response_json is None:
            # 上游返回非 JSON（如 SSE 流 / 纯文本错误），原样透传不丢数据
            await self.async_logger.warning(
                f"Non-stream response is not JSON (content-type={resp.headers.get('Content-Type')!r}), "
                f"captured {len(raw_bytes)} bytes as raw text for {call_id}"
            )

        if response_json is not None:
            if protocol == "openai-chat":
                stop_reason = ((response_json.get("choices") or [{}])[0].get("finish_reason"))
            elif protocol == "anthropic-messages":
                stop_reason = response_json.get("stop_reason")
            elif protocol == "openai-responses":
                stop_reason = response_json.get("status")
            else:
                stop_reason = None
        else:
            stop_reason = None

        if is_error_response_body(response_json):
            await self.async_logger.warning(
                f"Skip saving non-stream error response: {call_id}, "
                f"status={response_json.get('status')!r}, code={response_json.get('code')!r}, "
                f"error={bool(response_json.get('error'))}, success={response_json.get('success')!r}"
            )
            return response

        # 只保存上游 HTTP 200 且响应体不是业务错误的调用（非 JSON 时 response_body 用原始文本）
        try:
            raw_response = raw_bytes.decode("utf-8", errors="replace")
            raw_body = {"_raw": raw_response}
            if truncated:
                raw_body["_truncated"] = True
                raw_body["_captured_bytes"] = len(raw_bytes)
                raw_body["_capture_max_bytes"] = self.capture_max_bytes

            await self._queue_raw_save(
                call_id=call_id, protocol=protocol,
                request_path=orig_request.raw_path, request_body=raw_request_body,
                request_headers=list(orig_request.headers.items()), upstream_provider=host,
                upstream_model=model_id, client_model_alias=model_id,
                started_at=started_at, finished_at=datetime.now(),
                upstream_status=200, stop_reason=stop_reason,
                response_body=(response_json if response_json is not None
                                else raw_body),
                is_stream=False, **agent_meta,
            )
            await self.async_logger.info(f"Non-stream raw enqueue attempted: {call_id}")
        except Exception as e:
            await self.async_logger.error(f"Failed to save non-stream raw: {e}")

        return response

    # ========== 前端 Web UI ==========

    async def handle_index(self, request: web.Request) -> web.Response:
        """前端管理界面"""
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_html()
        resp = web.FileResponse(WEB_INDEX_FILE)
        _maybe_set_ui_cookie(resp, request, self.config)
        return resp

    async def handle_api_logs(self, request: web.Request) -> web.Response:
        """Return the most recent proxy log lines for the Web UI."""
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        try:
            limit = max(1, min(5000, int(request.query.get("limit", "500"))))
        except ValueError:
            return web.json_response({"error": "limit must be an integer"}, status=400)
        try:
            lines = await asyncio.to_thread(self._read_log_tail, limit)
        except OSError as exc:
            return web.json_response({"error": f"failed to read log: {exc}"}, status=500)
        return web.json_response({"lines": lines, "path": self.log_path, "limit": limit})

    def _read_log_tail(self, limit: int) -> list[str]:
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, "r", encoding="utf-8", errors="replace") as log_file:
            return [line.rstrip("\r\n") for line in log_file.readlines()[-limit:]]

    async def handle_api_logs_delete(self, request: web.Request) -> web.Response:
        """Clear the active proxy log without stopping the service."""
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        if self.async_logger is None:
            return web.json_response({"error": "logger is not ready"}, status=503)
        try:
            await asyncio.to_thread(self.async_logger.clear_file)
        except OSError as exc:
            return web.json_response({"error": f"failed to clear log: {exc}"}, status=500)
        return web.json_response({"cleared": True})

    async def handle_api_calls(self, request: web.Request) -> web.Response:
        """调用列表 API：支持分页、筛选"""
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        page = int(request.query.get("page", 1))
        page_size = int(request.query.get("page_size", 20))
        offset = (page - 1) * page_size
        try:
            where, params = _call_filter_clause(request.query)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        where_clause = (" WHERE " + " AND ".join(where)) if where else ""

        async with aiosqlite.connect(self.db_path) as conn:
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

    async def handle_api_filter_options(self, request: web.Request) -> web.Response:
        """Return distinct Host and Model values available in captured calls."""
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("""
                SELECT upstream_provider AS value, COUNT(*) AS count
                FROM calls
                WHERE upstream_provider IS NOT NULL AND TRIM(upstream_provider) != ''
                GROUP BY upstream_provider
                ORDER BY count DESC, value COLLATE NOCASE ASC
            """)
            hosts = [{"value": row[0], "count": row[1]} for row in await cursor.fetchall()]

            cursor = await conn.execute("""
                SELECT upstream_model AS value, COUNT(*) AS count
                FROM calls
                WHERE upstream_model IS NOT NULL AND TRIM(upstream_model) != ''
                GROUP BY upstream_model
                ORDER BY count DESC, value COLLATE NOCASE ASC
            """)
            models = [{"value": row[0], "count": row[1]} for row in await cursor.fetchall()]
        return web.json_response({"hosts": hosts, "models": models})

    async def handle_api_call_ids(self, request: web.Request) -> web.Response:
        """Return IDs for the current filters so the UI can select across pages."""
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        try:
            where, params = _call_filter_clause(request.query)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        where_clause = (" WHERE " + " AND ".join(where)) if where else ""

        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                f"SELECT call_id FROM calls{where_clause} ORDER BY started_at DESC",
                params,
            )
            call_ids = [row[0] for row in await cursor.fetchmany(MAX_SELECTED_CALLS + 1)]
        if len(call_ids) > MAX_SELECTED_CALLS:
            return web.json_response({
                "error": f"filter matches more than {MAX_SELECTED_CALLS} calls; narrow the filters"
            }, status=400)
        return web.json_response({"total": len(call_ids), "call_ids": call_ids})

    async def handle_api_call_detail(self, request: web.Request) -> web.Response:
        """调用详情 API：读取完整 JSON 文件"""
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        call_id = request.match_info["call_id"]

        async with aiosqlite.connect(self.db_path) as conn:
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

    async def handle_api_call_delete(self, request: web.Request) -> web.Response:
        """删除调用记录：同时删 DB 行和 JSON 文件"""
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        call_id = request.match_info["call_id"]

        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT raw_path FROM calls WHERE call_id = ?", (call_id,))
            row = await cursor.fetchone()
            if not row:
                return web.json_response({"error": "not found"}, status=404)

            raw_path = row["raw_path"]
            await conn.execute("DELETE FROM calls WHERE call_id = ?", (call_id,))
            await conn.commit()

        # 删除 JSON 文件（文件不存在不算错）
        if raw_path and os.path.exists(raw_path):
            try:
                os.remove(raw_path)
            except OSError as e:
                await self.async_logger.warning(f"Failed to remove {raw_path}: {e}")

        await self.async_logger.info(f"Call deleted: {call_id}")
        return web.json_response({"deleted": call_id})

    async def handle_api_calls_delete(self, request: web.Request) -> web.Response:
        """批量删除调用记录及其 JSON 文件。"""
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        try:
            payload = await request.json()
        except (json.JSONDecodeError, aiohttp.ContentTypeError):
            return web.json_response({"error": "request body must be JSON"}, status=400)
        call_ids = payload.get("call_ids") if isinstance(payload, dict) else None
        if not isinstance(call_ids, list) or not call_ids:
            return web.json_response({"error": "select at least one call"}, status=400)
        if len(call_ids) > MAX_SELECTED_CALLS:
            return web.json_response({
                "error": f"cannot delete more than {MAX_SELECTED_CALLS} calls at once"
            }, status=400)
        if any(not isinstance(call_id, str) or not call_id.strip() for call_id in call_ids):
            return web.json_response({"error": "call_ids must contain non-empty strings"}, status=400)
        call_ids = list(dict.fromkeys(call_ids))

        placeholders = ",".join("?" for _ in call_ids)
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"SELECT call_id, raw_path FROM calls WHERE call_id IN ({placeholders})",
                call_ids,
            )
            rows = await cursor.fetchall()
            if rows:
                await conn.execute(
                    f"DELETE FROM calls WHERE call_id IN ({placeholders})",
                    call_ids,
                )
                await conn.commit()

        deleted_ids = [row["call_id"] for row in rows]
        for row in rows:
            raw_path = row["raw_path"]
            if raw_path and os.path.exists(raw_path):
                try:
                    os.remove(raw_path)
                except OSError as e:
                    await self.async_logger.warning(f"Failed to remove {raw_path}: {e}")
        await self.async_logger.info(f"Calls deleted: {len(deleted_ids)}")
        return web.json_response({"deleted": deleted_ids, "deleted_count": len(deleted_ids)})

    async def handle_api_stats(self, request: web.Request) -> web.Response:
        """统计概览 API"""
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        async with aiosqlite.connect(self.db_path) as conn:
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

    async def handle_api_export_inspect(self, request: web.Request) -> web.Response:
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        try:
            if request.content_type != "application/json":
                return web.json_response({"error": "Content-Type must be application/json"}, status=415)
            payload = await request.json()
            if not isinstance(payload, dict) or "call_ids" not in payload:
                raise ExportValidationError("select at least one call")
            report = await asyncio.to_thread(
                inspect_for_web,
                self.db_path,
                limit=payload.get("limit"),
                call_ids=payload.get("call_ids"),
                include_window_budget=bool(payload.get("include_window_budget", True)),
                chars_per_token=payload.get("chars_per_token", 4.0),
            )
        except (ExportValidationError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(report)

    async def handle_api_export(self, request: web.Request) -> web.Response:
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        if request.content_type != "application/json":
            return web.json_response({"error": "Content-Type must be application/json"}, status=415)
        if self.export_lock.locked():
            return web.json_response({"error": "an export is already running"}, status=409)
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or "call_ids" not in payload:
                raise ExportValidationError("select at least one call")
            async with self.export_lock:
                result = await asyncio.to_thread(run_export, self.db_path, payload)
        except (ExportValidationError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            await self.async_logger.error(f"Dataset export failed: {e}\n{traceback.format_exc()}")
            return web.json_response({"error": "dataset export failed"}, status=500)
        result["download_url"] = "/api/exports/" + result["filename"]
        await self.async_logger.info(
            f"Dataset exported: {result['filename']}, written={result.get('written', 0)}"
        )
        return web.json_response(result, status=201)

    async def handle_api_exports(self, request: web.Request) -> web.Response:
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        rows = await asyncio.to_thread(list_exports, self.db_path)
        for row in rows:
            row["download_url"] = "/api/exports/" + row["filename"]
        return web.json_response({"data": rows})

    async def handle_api_export_download(self, request: web.Request) -> web.StreamResponse:
        if not verify_ui_auth(request, self.config):
            return _ui_unauthorized_json()
        filename = request.match_info["filename"]
        path = export_file_path(self.db_path, filename)
        if path is None or not os.path.isfile(path):
            return web.json_response({"error": "not found"}, status=404)
        return web.FileResponse(
            path,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


# ========== 前端资源路径 ==========

WEB_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "web"
WEB_INDEX_FILE = WEB_DIR / "index.html"
WEB_STATIC_DIR = WEB_DIR / "static"


# ========== 主函数 ==========

def _register_routes(server: "ProxyServer") -> None:
    """Register frontend + proxy routes on a server instance."""
    server.app.router.add_get("/", server.handle_index)
    server.app.router.add_get("/api/logs", server.handle_api_logs)
    server.app.router.add_delete("/api/logs", server.handle_api_logs_delete)
    server.app.router.add_get("/api/calls", server.handle_api_calls)
    server.app.router.add_get("/api/filter-options", server.handle_api_filter_options)
    server.app.router.add_get("/api/call-ids", server.handle_api_call_ids)
    server.app.router.add_get("/api/calls/{call_id}", server.handle_api_call_detail)
    server.app.router.add_delete("/api/calls", server.handle_api_calls_delete)
    server.app.router.add_delete("/api/calls/{call_id}", server.handle_api_call_delete)
    server.app.router.add_get("/api/stats", server.handle_api_stats)
    server.app.router.add_post("/api/export/inspect", server.handle_api_export_inspect)
    server.app.router.add_post("/api/export", server.handle_api_export)
    server.app.router.add_get("/api/exports", server.handle_api_exports)
    server.app.router.add_get("/api/exports/{filename}", server.handle_api_export_download)
    server.app.router.add_static("/static/", WEB_STATIC_DIR)
    server.app.router.add_route("*", "/{path_info:.*}", server.handle_proxy)


async def main():
    a = parse_args()
    logging.getLogger().setLevel(getattr(logging, a.log_level.upper()))
    server = ProxyServer(a.config, a.port, log_level=a.log_level, bind=a.bind)
    _register_routes(server)
    await server.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    return server


class ProxyHandle:
    """Handle to a proxy running in a background thread.

    Supports clean shutdown so the listening socket on the old port is released
    before a new proxy is started on a different port.
    """

    def __init__(self, thread, loop, server: "ProxyServer"):
        self.thread = thread
        self.loop = loop
        self.server = server

    def stop(self, timeout: float = 5.0) -> None:
        """Schedule runner cleanup on the proxy loop, then stop the loop and join."""
        if not self.thread or not self.thread.is_alive():
            return
        loop = self.loop
        server = self.server

        async def _shutdown():
            try:
                if server is not None:
                    await server.stop()
            except Exception as e:
                logger.error(f"Error during proxy shutdown: {e}")

        try:
            fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
            fut.result(timeout=timeout)
        except Exception as e:
            logger.error(f"Proxy shutdown coroutine failed: {e}")

        # break out of run_forever() so the thread can exit and close the loop
        loop.call_soon_threadsafe(loop.stop)
        self.thread.join(timeout=timeout)


def start_proxy_in_thread(port: int = 12345, config: str = "config.json",
                         log_level: str = "INFO", on_started=None,
                         bind: Optional[str] = None) -> "ProxyHandle":
    """Start the proxy in a background daemon thread (for embedding in tray apps).

    Returns a ProxyHandle whose stop() releases the listening socket and joins
    the worker thread, so the proxy can be cleanly restarted on a new port.
    """
    import threading

    # Create the loop in the caller thread; run it in the worker thread.
    # run_coroutine_threadsafe() can then schedule the shutdown coroutine on it.
    loop = asyncio.new_event_loop()
    state = {"server": None, "ready": threading.Event()}

    def _run():
        asyncio.set_event_loop(loop)
        try:
            server = ProxyServer(config, port, log_level=log_level, bind=bind)
            _register_routes(server)
            state["server"] = server
            loop.run_until_complete(server.start())
            state["ready"].set()
            if on_started:
                on_started()
            loop.run_forever()
        except Exception as e:
            logger.error(f"Proxy thread error: {e}")
            state["ready"].set()  # unblock waiter even on failure
        finally:
            try:
                if not loop.is_closed():
                    loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_run, name="proxy-loop", daemon=True)
    t.start()
    # block until the server is listening (or fails to start)
    state["ready"].wait(timeout=10.0)
    return ProxyHandle(t, loop, state["server"])


if __name__ == "__main__":
    asyncio.run(main())
