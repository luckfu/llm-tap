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
import aiohttp
import aiosqlite
from aiohttp import web
from aiohttp.resolver import DefaultResolver
from datetime import datetime
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
        await asyncio.to_thread(init_async_logger, "proxy", "proxy.log",
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
            timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_connect=30, sock_read=600)

            # GET 请求（如 /v1/models 模型列表）：纯透传，不保存
            if method == "GET":
                async with session.get(upstream_url, headers=upstream_headers, timeout=timeout) as resp:
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

            # Responses API 必须以 response.completed 作为完整响应边界。
            # 缺少该事件通常意味着上游流被截断、格式不兼容或请求失败；
            # 透传已经收到的内容，但不要把占位 note 当作训练数据持久化。
            if not should_persist_stream_response(protocol, completed_response):
                await self.async_logger.warning(
                    f"Skip saving incomplete Responses stream: {call_id}, "
                    "no response.completed captured"
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
        resp = web.Response(text=INDEX_HTML, content_type="text/html")
        _maybe_set_ui_cookie(resp, request, self.config)
        return resp

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


# ========== 前端 HTML ==========

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light">
<title>llm-tap · 数据采集管理</title>
<link rel="icon" type="image/svg+xml" href='data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"%3E%3Cdefs%3E%3ClinearGradient id="g" x1="0" y1="0" x2="0" y2="1"%3E%3Cstop offset="0" stop-color="%23a0e1f0"/%3E%3Cstop offset="1" stop-color="%231e7daa"/%3E%3C/linearGradient%3E%3C/defs%3E%3Cpath fill="url(%23g)" d="M32 5C24 17 15 27 15 40c0 10 8 18 17 18s17-8 17-18C49 27 40 17 32 5z"/%3E%3Cellipse cx="26" cy="34" rx="6" ry="9" fill="white" opacity=".35" transform="rotate(24 26 34)"/%3E%3C/svg%3E'>
<style>
:root {
  --ink: #172126;
  --muted: #66757c;
  --subtle: #87939a;
  --line: #dce3e5;
  --line-strong: #c7d1d4;
  --canvas: #f3f6f5;
  --surface: #ffffff;
  --surface-soft: #f8faf9;
  --header: #141b1f;
  --header-soft: #20292e;
  --accent: #087f75;
  --accent-strong: #06675f;
  --accent-soft: #e7f5f2;
  --success: #16845b;
  --success-soft: #e8f5ee;
  --danger: #c6414d;
  --danger-soft: #fff0f1;
  --info: #246d9b;
  --info-soft: #eaf4fa;
  --warning: #9a6b13;
  --shadow-sm: 0 1px 2px rgba(20, 33, 38, .05), 0 4px 14px rgba(20, 33, 38, .04);
  --shadow-lg: 0 24px 64px rgba(10, 22, 28, .18);
}
* { margin: 0; padding: 0; box-sizing: border-box; letter-spacing: 0; }
html { min-width: 320px; background: var(--canvas); }
body {
  min-height: 100vh;
  color: var(--ink);
  background: var(--canvas);
  font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
button, input, select { font: inherit; letter-spacing: 0; }
button, select { cursor: pointer; }
button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible, a:focus-visible {
  outline: 3px solid rgba(8, 127, 117, .2);
  outline-offset: 2px;
}
.navbar {
  position: sticky;
  top: 0;
  z-index: 100;
  min-height: 72px;
  color: #fff;
  background: var(--header);
  border-bottom: 1px solid rgba(255, 255, 255, .08);
}
.navbar-inner {
  width: min(100%, 1480px);
  min-height: 72px;
  margin: 0 auto;
  padding: 0 28px;
  display: flex;
  align-items: center;
  gap: 28px;
}
.brand { display: flex; align-items: center; gap: 11px; flex: 0 0 auto; }
.brand-mark {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border-radius: 7px;
  color: #fff;
  background: var(--accent);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .16);
  font-size: 15px;
  font-weight: 750;
}
.brand-copy { display: flex; flex-direction: column; line-height: 1.15; }
.brand-copy strong { font-size: 16px; font-weight: 720; }
.brand-copy span { margin-top: 4px; color: #94a4aa; font-size: 11px; }
.nav-tabs { min-height: 72px; display: flex; align-items: stretch; gap: 4px; }
.nav-tab {
  position: relative;
  min-width: 88px;
  padding: 0 16px;
  border: 0;
  color: #9eacb2;
  background: transparent;
  font-size: 14px;
  font-weight: 600;
}
.nav-tab::after {
  content: "";
  position: absolute;
  left: 16px;
  right: 16px;
  bottom: 0;
  height: 3px;
  background: transparent;
}
.nav-tab:hover { color: #fff; background: rgba(255, 255, 255, .035); }
.nav-tab.active { color: #fff; }
.nav-tab.active::after { background: #31b9aa; }
.navbar-actions { margin-left: auto; display: flex; align-items: center; gap: 14px; }
.capture-state { display: flex; align-items: center; gap: 7px; color: #aebbc0; font-size: 12px; white-space: nowrap; }
.capture-state::before {
  content: "";
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #38b77d;
  box-shadow: 0 0 0 4px rgba(56, 183, 125, .12);
}
.lang-switch {
  display: flex;
  gap: 2px;
  padding: 3px;
  border: 1px solid #344147;
  border-radius: 6px;
  background: #101619;
}
.lang-switch button {
  min-width: 34px;
  height: 28px;
  padding: 0 8px;
  border: 0;
  border-radius: 4px;
  color: #7f9097;
  background: transparent;
  font-size: 12px;
  font-weight: 650;
}
.lang-switch button:hover { color: #fff; }
.lang-switch button.active { color: #fff; background: var(--header-soft); }
.container { width: min(100%, 1480px); margin: 0 auto; padding: 30px 28px 56px; }
.view-header {
  min-height: 48px;
  margin-bottom: 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
}
.view-heading { min-width: 0; }
.eyebrow { display: block; margin-bottom: 4px; color: var(--accent); font-size: 11px; font-weight: 750; text-transform: uppercase; }
.view-header h1 { font-size: 24px; line-height: 1.2; font-weight: 720; }
.icon-btn {
  width: 38px;
  height: 38px;
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  border: 1px solid var(--line-strong);
  border-radius: 7px;
  color: #526168;
  background: var(--surface);
  box-shadow: var(--shadow-sm);
  font-size: 20px;
  line-height: 1;
}
.icon-btn:hover { color: var(--accent); border-color: #90bdb7; background: var(--accent-soft); }
.filters {
  display: grid;
  grid-template-columns: minmax(170px, 1.15fr) 190px minmax(170px, 1fr) 165px auto;
  gap: 10px;
  margin-bottom: 14px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.time-range {
  grid-column: 1 / 5;
  display: grid;
  grid-template-columns: minmax(210px, 1fr) minmax(210px, 1fr) auto;
  gap: 10px;
  align-items: end;
}
.time-field { min-width: 0; }
.time-field label { display: block; margin: 0 0 5px 2px; color: var(--muted); font-size: 11px; font-weight: 650; }
.time-clear-btn {
  width: 42px;
  min-width: 42px !important;
  padding: 0 !important;
  border: 1px solid var(--line-strong) !important;
  color: #5f6e74 !important;
  background: var(--surface) !important;
  box-shadow: none !important;
  font-size: 20px !important;
  font-weight: 400 !important;
}
.time-clear-btn:hover { border-color: #91a0a5 !important; color: var(--ink) !important; background: var(--surface-soft) !important; }
.filters select, .filters input,
.form-field select, .form-field input[type="number"] {
  width: 100%;
  height: 42px;
  min-width: 0;
  padding: 0 12px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  color: var(--ink);
  background: var(--surface);
  font-size: 14px;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.filters input::placeholder, .form-field input::placeholder { color: #98a3a8; }
.filters select:hover, .filters input:hover,
.form-field select:hover, .form-field input[type="number"]:hover { border-color: #a7b4b8; }
.filters select:focus, .filters input:focus,
.form-field select:focus, .form-field input[type="number"]:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(8, 127, 117, .1);
  outline: 0;
}
.filters button, .primary-btn, .secondary-btn, .compact-btn, .danger-btn, .pagination button {
  min-height: 38px;
  border-radius: 6px;
  padding: 0 14px;
  font-size: 13px;
  font-weight: 650;
  transition: background .15s ease, border-color .15s ease, color .15s ease, transform .15s ease;
}
.filters button, .primary-btn {
  border: 1px solid var(--accent);
  color: #fff;
  background: var(--accent);
  box-shadow: 0 1px 2px rgba(8, 87, 80, .18);
}
.filters button { min-width: 88px; }
.filters button:hover, .primary-btn:hover { border-color: var(--accent-strong); background: var(--accent-strong); }
.secondary-btn, .compact-btn, .pagination button {
  border: 1px solid var(--line-strong);
  color: #435158;
  background: var(--surface);
}
.secondary-btn:hover, .compact-btn:hover, .pagination button:hover:not(:disabled) { border-color: #91a0a5; color: var(--ink); background: var(--surface-soft); }
.table-wrap {
  min-width: 0;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.selection-toolbar {
  min-height: 48px;
  margin-bottom: 14px;
  padding: 8px 10px 8px 14px;
  display: flex;
  align-items: center;
  gap: 9px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.selection-count { margin-right: auto; color: #405057; font-size: 13px; font-weight: 680; }
.selection-count strong { color: var(--accent); font-size: 16px; }
.selection-toolbar button {
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  color: #435158;
  background: var(--surface);
  font-size: 12px;
  font-weight: 650;
}
.selection-toolbar button:hover:not(:disabled) { border-color: #91a0a5; background: var(--surface-soft); }
.selection-toolbar .selection-primary { border-color: var(--accent); color: #fff; background: var(--accent); }
.selection-toolbar .selection-primary:hover:not(:disabled) { border-color: var(--accent-strong); background: var(--accent-strong); }
.select-cell { width: 46px; padding-left: 14px; padding-right: 6px; text-align: center; }
.select-cell input { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }
tbody tr.is-selected { background: #eff8f6; }
table { width: 100%; border-collapse: collapse; }
#view-list table { min-width: 1080px; }
th {
  height: 44px;
  padding: 0 16px;
  border-bottom: 1px solid var(--line);
  color: #66747a;
  background: var(--surface-soft);
  text-align: left;
  white-space: nowrap;
  font-size: 11px;
  font-weight: 750;
  text-transform: uppercase;
}
td {
  height: 54px;
  padding: 9px 16px;
  border-bottom: 1px solid #e9eeee;
  color: #334047;
  font-size: 13px;
  white-space: nowrap;
}
tbody tr:last-child td { border-bottom: 0; }
tbody tr { transition: background .12s ease; }
tbody tr:hover { background: #f7faf9; cursor: pointer; }
.badge, .tag {
  display: inline-flex;
  align-items: center;
  min-height: 23px;
  padding: 2px 8px;
  border-radius: 5px;
  font-size: 11px;
  font-weight: 650;
  white-space: nowrap;
}
.badge-success { color: #126342; background: var(--success-soft); }
.badge-error { color: #9e2f3a; background: var(--danger-soft); }
.badge-stream { color: #225f85; background: var(--info-soft); }
.tag { color: #4b5b62; background: #edf1f2; }
.action-cell { width: 84px; text-align: right; }
.danger-btn {
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid #e2a9ae;
  color: var(--danger);
  background: transparent;
  font-size: 12px;
}
.danger-btn:hover { border-color: var(--danger); color: #fff; background: var(--danger); }
button:disabled { opacity: .5; cursor: not-allowed; transform: none; }
.pagination { margin-top: 14px; display: flex; align-items: center; justify-content: space-between; gap: 16px; color: var(--muted); font-size: 12px; }
.pagination div { display: flex; gap: 8px; }
.pagination button { min-height: 34px; }
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-bottom: 22px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.stat-card { min-width: 0; padding: 20px 22px; border-right: 1px solid var(--line); }
.stat-card:last-child { border-right: 0; }
.stat-card .label { margin-bottom: 9px; color: var(--muted); font-size: 12px; font-weight: 600; }
.stat-card .value { color: var(--ink); font-size: 27px; line-height: 1.15; font-weight: 720; font-variant-numeric: tabular-nums; }
.stat-card .value span { margin-left: 4px; color: var(--subtle) !important; font-size: 12px !important; font-weight: 600; }
.stat-card .value.metric-success { color: var(--success) !important; }
.metric-success { color: var(--success) !important; }
.stats-layout { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(320px, .9fr); gap: 18px; align-items: start; }
.stats-side { display: grid; gap: 18px; }
.section-block { min-width: 0; }
.section-heading { margin: 0 0 10px; color: #46545a; font-size: 13px; font-weight: 700; }
.panel {
  min-width: 0;
  padding: 22px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}
.panel-heading { margin-bottom: 20px; display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.panel-heading h2 { font-size: 15px; font-weight: 720; }
.panel-heading-meta { color: var(--subtle); font-size: 11px; }
.export-layout { display: grid; grid-template-columns: minmax(340px, 440px) minmax(0, 1fr); gap: 18px; align-items: start; }
.export-side { display: grid; gap: 18px; min-width: 0; }
.selection-scope {
  margin-bottom: 18px;
  padding: 13px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border: 1px solid #bcd9d4;
  border-radius: 7px;
  background: var(--accent-soft);
}
.selection-scope.is-empty { border-color: #e5c98c; background: #fff8e9; }
.selection-scope strong { display: block; color: #31504d; font-size: 13px; }
.selection-scope.is-empty strong { color: #7e5a13; }
.selection-scope span { display: block; margin-top: 2px; color: var(--muted); font-size: 11px; }
.selection-scope button {
  flex: 0 0 auto;
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid #96bdb7;
  border-radius: 6px;
  color: var(--accent-strong);
  background: #fff;
  font-size: 12px;
  font-weight: 650;
}
.form-field { margin-bottom: 17px; }
.form-field > label { display: block; margin-bottom: 7px; color: #4d5b61; font-size: 12px; font-weight: 680; }
.field-label, .option-label {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.field-label { margin-bottom: 7px; }
.field-label label {
  min-width: 0;
  color: #4d5b61;
  font-size: 12px;
  font-weight: 680;
}
.option-label label { min-width: 0; cursor: pointer; }
.hint-trigger {
  position: relative;
  width: 18px;
  height: 18px;
  flex: 0 0 18px;
  display: inline-grid;
  place-items: center;
  padding: 0;
  border: 1px solid #aebbc0;
  border-radius: 50%;
  color: #6d7d83;
  background: transparent;
  font: 700 11px/1 ui-sans-serif, sans-serif;
  cursor: help;
  z-index: 4;
}
.hint-trigger:hover, .hint-trigger:focus-visible, .hint-trigger[aria-expanded="true"] {
  color: #fff;
  border-color: var(--accent);
  background: var(--accent);
}
.hint-trigger::after {
  content: attr(data-tooltip);
  position: absolute;
  left: 50%;
  bottom: calc(100% + 9px);
  width: max-content;
  max-width: min(300px, calc(100vw - 32px));
  padding: 9px 11px;
  border: 1px solid #35434a;
  border-radius: 6px;
  color: #f5f8f8;
  background: #202a2f;
  box-shadow: 0 10px 28px rgba(12, 24, 30, .2);
  font-size: 11px;
  font-weight: 500;
  line-height: 1.55;
  text-align: left;
  white-space: normal;
  overflow-wrap: break-word;
  pointer-events: none;
  opacity: 0;
  transform: translate(-50%, 4px);
  transition: opacity .14s ease, transform .14s ease;
}
.hint-trigger::before {
  content: "";
  position: absolute;
  left: 50%;
  bottom: calc(100% + 4px);
  width: 9px;
  height: 9px;
  border-right: 1px solid #35434a;
  border-bottom: 1px solid #35434a;
  background: #202a2f;
  pointer-events: none;
  opacity: 0;
  transform: translateX(-50%) rotate(45deg);
}
.hint-trigger:hover::after, .hint-trigger:focus-visible::after, .hint-trigger[aria-expanded="true"]::after,
.hint-trigger:hover::before, .hint-trigger:focus-visible::before, .hint-trigger[aria-expanded="true"]::before {
  opacity: 1;
}
.hint-trigger:hover::after, .hint-trigger:focus-visible::after, .hint-trigger[aria-expanded="true"]::after {
  transform: translate(-50%, 0);
}
.field-help, .export-note { color: var(--muted); font-size: 12px; line-height: 1.6; }
.check-row {
  min-height: 38px;
  margin: 2px 0;
  display: flex;
  align-items: center;
  gap: 10px;
  color: #3e4c52;
  font-size: 13px;
  cursor: pointer;
}
.check-row input { width: 16px; height: 16px; accent-color: var(--accent); }
.check-row:has(input:disabled) { color: var(--subtle); }
.check-row:has(input:disabled) .option-label label { cursor: not-allowed; }
.switch-row {
  min-height: 50px;
  margin: 13px 0 4px;
  padding: 0 12px;
  justify-content: space-between;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: var(--surface-soft);
}
.switch-row .option-label { flex: 1 1 auto; }
.switch-row input {
  appearance: none;
  width: 36px;
  height: 20px;
  flex: 0 0 auto;
  margin-left: auto;
  border-radius: 10px;
  background: #bcc7ca;
  position: relative;
  transition: background .18s ease;
}
.switch-row input::after {
  content: "";
  position: absolute;
  top: 3px;
  left: 3px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, .2);
  transition: transform .18s ease;
}
.switch-row input:checked { background: var(--accent); }
.switch-row input:checked::after { transform: translateX(16px); }
.constraint-panel {
  margin: 10px 0 0;
  padding: 15px;
  border-left: 3px solid var(--accent);
  border-radius: 0 7px 7px 0;
  background: #f2f8f6;
}
.constraint-header { margin-bottom: 8px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.constraint-header strong { color: #314b49; font-size: 13px; }
.constraint-status { margin-bottom: 14px; color: #56706e; font-size: 11px; line-height: 1.6; }
.compact-btn { min-height: 30px; padding: 0 9px; white-space: nowrap; font-size: 11px; }
.export-actions { display: grid; grid-template-columns: 1fr 1.25fr; gap: 9px; margin-top: 20px; }
.primary-btn, .secondary-btn { min-height: 42px; }
.is-loading::before {
  content: "";
  width: 12px;
  height: 12px;
  display: inline-block;
  margin-right: 8px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  vertical-align: -2px;
  animation: spin .7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.export-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin: 0 -22px 17px;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}
.summary-item { min-width: 0; padding: 17px 22px; border-right: 1px solid var(--line); }
.summary-item:last-child { border-right: 0; }
.summary-item span { display: block; margin-bottom: 7px; color: var(--muted); font-size: 11px; }
.summary-item strong { color: var(--ink); font-size: 22px; line-height: 1.1; font-weight: 720; font-variant-numeric: tabular-nums; }
.status-box { display: none; margin: 14px 0 0; padding: 12px 13px; border-radius: 6px; font-size: 12px; line-height: 1.55; }
.status-box.show { display: block; }
.status-info { border: 1px solid #c6dfeb; color: #245f7f; background: var(--info-soft); }
.status-success { border: 1px solid #badfc9; color: #176442; background: var(--success-soft); }
.status-error { border: 1px solid #efc3c7; color: #922f39; background: var(--danger-soft); }
.download-link { color: var(--accent); text-decoration: none; font-weight: 680; }
.download-link:hover { color: var(--accent-strong); text-decoration: underline; }
.exports-table { min-width: 620px; }
.exports-table td { vertical-align: middle; }
.exports-table td:first-child { max-width: 420px; overflow: hidden; text-overflow: ellipsis; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.exports-table-wrap { overflow-x: auto; border-radius: 6px; box-shadow: none; }
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: none;
  padding: 32px 20px;
  overflow-y: auto;
  background: rgba(10, 18, 22, .62);
  backdrop-filter: blur(3px);
}
.modal-overlay.show { display: flex; align-items: flex-start; justify-content: center; }
.modal {
  width: min(100%, 1060px);
  max-height: calc(100vh - 64px);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  border: 1px solid rgba(255, 255, 255, .35);
  border-radius: 8px;
  background: var(--surface);
  box-shadow: var(--shadow-lg);
}
.modal-header { min-height: 64px; padding: 0 20px 0 24px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.modal-header h2 { font-size: 16px; font-weight: 720; }
.modal-title-group { min-width: 0; display: flex; align-items: center; gap: 14px; }
.modal-close { width: 36px; height: 36px; border: 0; border-radius: 6px; color: #708087; background: transparent; font-size: 25px; line-height: 1; }
.modal-close:hover { color: var(--ink); background: #edf1f2; }
.modal-body { padding: 22px 24px 28px; overflow-y: auto; }
.detail-section { margin-bottom: 12px; border: 1px solid var(--line); border-radius: 7px; overflow: hidden; }
.detail-section summary { padding: 12px 14px; color: #4b5a60; background: var(--surface-soft); cursor: pointer; user-select: none; font-size: 12px; font-weight: 700; text-transform: uppercase; }
.detail-section summary:hover { color: var(--accent); }
.detail-section pre { max-width: 100%; padding: 16px; overflow-x: auto; color: #304047; background: #f7f9f8; font: 12px/1.65 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 1080px) {
  .filters { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .filters button { grid-column: span 4; }
  .time-range { grid-column: span 4; }
  .time-range .time-clear-btn { grid-column: auto; }
  .stats-layout { grid-template-columns: 1fr; }
  .stats-side { grid-template-columns: 1fr 1fr; }
  .export-layout { grid-template-columns: minmax(320px, 390px) minmax(0, 1fr); }
}
@media (max-width: 860px) {
  .navbar-inner { min-height: auto; padding: 12px 18px 0; flex-wrap: wrap; gap: 10px 18px; }
  .brand-copy span, .capture-state { display: none; }
  .nav-tabs { order: 3; width: 100%; min-height: 46px; }
  .nav-tab { min-height: 46px; flex: 1; }
  .container { padding: 24px 18px 44px; }
  .export-layout { grid-template-columns: minmax(0, 1fr); }
  .stats-grid { grid-template-columns: 1fr 1fr; }
  .stat-card:nth-child(2) { border-right: 0; }
  .stat-card:nth-child(-n+2) { border-bottom: 1px solid var(--line); }
}
@media (max-width: 620px) {
  .navbar-inner { padding-left: 14px; padding-right: 14px; }
  .brand-mark { width: 32px; height: 32px; }
  .brand-copy strong { font-size: 15px; }
  .navbar-actions { gap: 8px; }
  .nav-tab { min-width: 0; padding: 0 8px; font-size: 13px; }
  .nav-tab::after { left: 9px; right: 9px; }
  .container { padding: 20px 12px 36px; }
  .view-header { margin-bottom: 14px; }
  .view-header h1 { font-size: 21px; }
  .filters { grid-template-columns: 1fr 1fr; padding: 10px; }
  .filters input:first-child, .filters input:nth-child(3), .filters button { grid-column: span 2; }
  .time-range { grid-column: span 2; grid-template-columns: 1fr; }
  .time-range .time-clear-btn { width: 100%; grid-column: auto; }
  .selection-toolbar { align-items: stretch; flex-wrap: wrap; }
  .selection-count { width: 100%; }
  .selection-toolbar button { flex: 1 1 auto; }
  .selection-scope { align-items: flex-start; flex-direction: column; }
  .stats-grid { margin-bottom: 16px; }
  .stat-card { padding: 16px; }
  .stat-card .value { font-size: 23px; }
  .stats-side { grid-template-columns: 1fr; }
  .panel { padding: 18px 15px; }
  .export-summary { margin-left: -15px; margin-right: -15px; }
  .summary-item { padding: 15px 12px; }
  .summary-item strong { font-size: 19px; }
  .export-actions { grid-template-columns: 1fr; }
  .constraint-header { align-items: flex-start; flex-direction: column; }
  .hint-trigger::after {
    position: fixed;
    left: 16px;
    right: 16px;
    bottom: 16px;
    width: auto;
    max-width: none;
    z-index: 1200;
    transform: translateY(6px);
    font-size: 12px;
  }
  .hint-trigger::before { display: none; }
  .hint-trigger:hover::after, .hint-trigger:focus-visible::after, .hint-trigger[aria-expanded="true"]::after { transform: translateY(0); }
  .pagination { align-items: flex-start; flex-direction: column; }
  .pagination div { width: 100%; }
  .pagination button { flex: 1; }
  .modal-overlay { padding: 0; }
  .modal { min-height: 100vh; max-height: 100vh; border: 0; border-radius: 0; }
  .modal-header { padding: 0 12px 0 16px; }
  .modal-body { padding: 14px 12px 20px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; animation-duration: .01ms !important; }
}
</style>
</head>
<body>
<header class="navbar">
  <div class="navbar-inner">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">LT</div>
      <div class="brand-copy">
        <strong>llm-tap</strong>
        <span data-i18n="brandProduct">Capture Console</span>
      </div>
    </div>
    <nav class="nav-tabs" aria-label="Primary navigation">
      <button type="button" id="tab-list" class="nav-tab active" onclick="switchTab('list')" data-i18n="tabList">调用列表</button>
      <button type="button" id="tab-stats" class="nav-tab" onclick="switchTab('stats')" data-i18n="tabStats">统计概览</button>
      <button type="button" id="tab-export" class="nav-tab" onclick="switchTab('export')" data-i18n="tabExport">数据导出</button>
    </nav>
    <div class="navbar-actions">
      <span class="capture-state" data-i18n="captureActive">本地采集中</span>
      <div class="lang-switch" aria-label="Language">
        <button type="button" id="lang-zh" onclick="setLang('zh')">中</button>
        <button type="button" id="lang-en" class="active" onclick="setLang('en')">EN</button>
      </div>
    </div>
  </div>
</header>

<main class="container">
  <section id="view-list">
    <div class="view-header">
      <div class="view-heading">
        <span class="eyebrow" data-i18n="callsEyebrow">Capture ledger</span>
        <h1 data-i18n="callsTitle">调用记录</h1>
      </div>
      <button type="button" id="calls-refresh" class="icon-btn" onclick="refreshCallsView()" data-i18n-title="refresh" title="刷新" aria-label="刷新">↻</button>
    </div>
    <div class="filters">
      <select id="f-host" onchange="applyCallFilters()">
        <option value="" data-i18n="allHosts">全部 Host</option>
      </select>
      <select id="f-protocol" onchange="applyCallFilters()">
        <option value="" data-i18n="allProtocols">全部协议</option>
        <option value="openai-chat">OpenAI Chat</option>
        <option value="anthropic-messages">Anthropic Messages</option>
        <option value="openai-responses">OpenAI Responses</option>
        <option value="embeddings">Embeddings</option>
        <option value="rerank">Rerank</option>
      </select>
      <select id="f-model" onchange="applyCallFilters()">
        <option value="" data-i18n="allModels">全部模型</option>
      </select>
      <select id="f-status" onchange="applyCallFilters()">
        <option value="" data-i18n="allStatus">全部状态</option>
        <option value="success" data-i18n="success">成功</option>
        <option value="error" data-i18n="error">失败</option>
      </select>
      <button onclick="applyCallFilters()" data-i18n="search">查询</button>
      <div class="time-range">
        <div class="time-field"><label for="f-start-time" data-i18n="startTime">开始时间</label><input id="f-start-time" type="datetime-local" step="60" onchange="applyCallFilters()"></div>
        <div class="time-field"><label for="f-end-time" data-i18n="endTime">结束时间</label><input id="f-end-time" type="datetime-local" step="60" onchange="applyCallFilters()"></div>
        <button type="button" class="time-clear-btn" onclick="clearTimeRange()" data-i18n-title="clearTimeRange" title="清除时间范围" aria-label="清除时间范围">×</button>
      </div>
    </div>
    <div class="selection-toolbar">
      <div class="selection-count"><strong id="selected-count">0</strong> <span data-i18n="recordsSelected">条记录已选择</span></div>
      <button type="button" id="select-page-btn" onclick="selectCurrentPage()" data-i18n="selectPage">选择当前页</button>
      <button type="button" id="select-filtered-btn" onclick="selectFilteredCalls()" data-i18n="selectFiltered">选择全部筛选结果</button>
      <button type="button" id="clear-selection-btn" onclick="clearSelection()" data-i18n="clearSelection" disabled>清空</button>
      <button type="button" id="export-selected-btn" class="selection-primary" onclick="openSelectedExport()" data-i18n="exportSelected" disabled>导出所选</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="select-cell"><input id="select-page-checkbox" type="checkbox" onchange="toggleCurrentPage(this.checked)" data-i18n-title="selectPage" aria-label="Select current page"></th>
            <th data-i18n="colTime">时间</th><th>Host</th><th data-i18n="colProtocol">协议</th><th data-i18n="colModel">模型</th>
            <th data-i18n="colStatus">状态</th><th data-i18n="colDuration">耗时</th><th data-i18n="colFirstToken">首Token</th><th data-i18n="colStream">流式</th>
            <th class="action-cell" data-i18n="colActions">操作</th>
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
  </section>

  <section id="view-stats" style="display:none">
    <div class="view-header">
      <div class="view-heading">
        <span class="eyebrow" data-i18n="statsEyebrow">Operations</span>
        <h1 data-i18n="statsTitle">运行概览</h1>
      </div>
      <button type="button" id="stats-refresh" class="icon-btn" onclick="loadStats()" data-i18n-title="refresh" title="刷新" aria-label="刷新">↻</button>
    </div>
    <div id="stats-overview" class="stats-grid"></div>
    <div class="stats-layout">
      <section class="section-block">
        <h2 class="section-heading" data-i18n="statsByHost">按 Host 统计</h2>
        <div class="table-wrap"><table><thead><tr><th>Host</th><th data-i18n="colCount">调用数</th><th data-i18n="success">成功</th><th data-i18n="colAvgDuration">平均耗时(ms)</th></tr></thead><tbody id="stats-host"></tbody></table></div>
      </section>
      <div class="stats-side">
        <section class="section-block">
          <h2 class="section-heading" data-i18n="statsByProtocol">按协议统计</h2>
          <div class="table-wrap"><table><thead><tr><th data-i18n="colProtocol">协议</th><th data-i18n="colCount">调用数</th></tr></thead><tbody id="stats-protocol"></tbody></table></div>
        </section>
        <section class="section-block">
          <h2 class="section-heading" data-i18n="statsByModel">按模型统计</h2>
          <div class="table-wrap"><table><thead><tr><th data-i18n="colModel">模型</th><th data-i18n="colCount">调用数</th></tr></thead><tbody id="stats-model"></tbody></table></div>
        </section>
      </div>
    </div>
  </section>

  <section id="view-export" style="display:none">
    <div class="view-header">
      <div class="view-heading">
        <span class="eyebrow" data-i18n="exportEyebrow">Dataset workspace</span>
        <h1 data-i18n="exportTitle">训练数据导出</h1>
      </div>
      <button type="button" id="export-refresh" class="icon-btn" onclick="refreshExportView()" data-i18n-title="refresh" title="刷新" aria-label="刷新">↻</button>
    </div>
    <div class="export-layout">
      <section class="panel">
        <div class="panel-heading"><h2 data-i18n="exportSettings">导出设置</h2></div>
        <div id="selection-scope" class="selection-scope is-empty">
          <div><strong id="selection-scope-title"></strong><span id="selection-scope-note"></span></div>
          <button type="button" onclick="switchTab('list')" data-i18n="changeSelection">修改选择</button>
        </div>
        <div class="form-field">
          <div class="field-label">
            <label for="export-format" data-i18n="exportFormat">数据格式</label>
            <button type="button" class="hint-trigger" data-hint="exportFormatHint" aria-expanded="false">i</button>
          </div>
          <select id="export-format" onchange="updateExportFields()">
            <option value="canonical">Canonical JSONL</option>
            <option value="tool_sft">Tool SFT JSONL</option>
            <option value="openai">OpenAI Messages JSONL</option>
            <option value="sharegpt">ShareGPT JSON</option>
          </select>
          <div id="format-description" class="field-help" style="margin-top:7px"></div>
        </div>
        <div class="check-row">
          <input id="export-metadata" type="checkbox">
          <div class="option-label"><label for="export-metadata" data-i18n="includeMetadata">包含溯源元数据</label><button type="button" class="hint-trigger" data-hint="includeMetadataHint" aria-expanded="false">i</button></div>
        </div>
        <div class="check-row">
          <input id="export-skipped" type="checkbox">
          <div class="option-label"><label for="export-skipped" data-i18n="includeSkipped">包含低质量样本</label><button type="button" class="hint-trigger" data-hint="includeSkippedHint" aria-expanded="false">i</button></div>
        </div>
        <div id="sharegpt-tools-row" class="check-row" style="display:none">
          <input id="export-tools" type="checkbox" checked>
          <div class="option-label"><label for="export-tools" data-i18n="includeTools">注入工具定义</label><button type="button" class="hint-trigger" data-hint="includeToolsHint" aria-expanded="false">i</button></div>
        </div>
        <div class="check-row switch-row">
          <div class="option-label"><label for="export-context-limit" data-i18n="enableContextLimit">限制每条记录上下文</label><button type="button" class="hint-trigger" data-hint="contextLimitHint" aria-expanded="false">i</button></div>
          <input id="export-context-limit" type="checkbox" onchange="updateExportFields()">
        </div>
        <div id="window-options" style="display:none">
          <div class="constraint-panel">
            <div class="constraint-header">
              <strong data-i18n="contextConstraint">单条记录上下文约束</strong>
              <button id="use-recommended-btn" type="button" class="compact-btn" onclick="useRecommendedBudget()" data-i18n="useRecommended" disabled>使用最大目标预算</button>
            </div>
            <div id="context-limit-status" class="constraint-status"></div>
            <div class="form-field">
              <div class="field-label"><label for="export-max-seq" data-i18n="maxSeqLen">每条记录最大估算 Token</label><button type="button" class="hint-trigger" data-hint="maxSeqLenHint" aria-expanded="false">i</button></div>
              <input id="export-max-seq" type="number" min="128" max="1000000" value="4096" oninput="updateContextConstraintStatus()">
            </div>
            <div class="form-field">
              <div class="field-label"><label for="export-chars-token" data-i18n="charsPerToken">字符 / Token</label><button type="button" class="hint-trigger" data-hint="charsPerTokenHint" aria-expanded="false">i</button></div>
              <input id="export-chars-token" type="number" min="0.1" max="100" step="0.1" value="4.0" oninput="updateContextConstraintStatus()">
            </div>
            <div class="form-field" style="margin-bottom:0">
              <div class="field-label"><label for="export-prefix-ratio" data-i18n="prefixRatio">固定前缀最多占比</label><button type="button" class="hint-trigger" data-hint="prefixRatioHint" aria-expanded="false">i</button></div>
              <input id="export-prefix-ratio" type="number" min="0" max="1" step="0.05" value="0.45">
            </div>
          </div>
        </div>
        <div class="export-actions">
          <button id="inspect-btn" class="secondary-btn" onclick="loadExportInspect(true)" data-i18n="inspectData">检查数据</button>
          <button id="export-btn" class="primary-btn" onclick="createExport()" data-i18n="startExport">开始导出</button>
        </div>
        <div id="export-status" class="status-box"></div>
      </section>

      <div class="export-side">
        <section class="panel">
          <div class="panel-heading"><h2 data-i18n="datasetOverview">数据概况</h2></div>
          <div id="export-summary" class="export-summary"></div>
          <div id="window-budget-note" class="export-note"></div>
        </section>
        <section class="panel">
          <div class="panel-heading"><h2 data-i18n="generatedFiles">最近生成</h2></div>
          <div class="table-wrap exports-table-wrap">
            <table class="exports-table">
              <thead><tr><th data-i18n="fileName">文件</th><th data-i18n="fileSize">大小</th><th data-i18n="createdTime">生成时间</th><th data-i18n="download">下载</th></tr></thead>
              <tbody id="exports-tbody"></tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  </section>
</main>

<div class="modal-overlay" id="modal" aria-hidden="true" onclick="if(event.target===this)closeModal()">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <div class="modal-header">
      <div class="modal-title-group">
        <h2 id="modal-title" data-i18n="callDetail">调用详情</h2>
        <button id="modal-delete-btn" class="danger-btn" onclick="deleteCall(event, currentDetailCallId, true)" data-i18n="delete">删除</button>
      </div>
      <button type="button" class="modal-close" onclick="closeModal()" data-i18n-title="close" title="关闭" aria-label="关闭">&times;</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
const i18n = {
  zh: {
    title: '数据采集', brandProduct: '采集控制台', captureActive: '本地采集中',
    tabList: '调用列表', tabStats: '统计概览', tabExport: '数据导出',
    callsEyebrow: '采集台账', callsTitle: '调用记录', statsEyebrow: '运行分析', statsTitle: '运行概览',
    exportEyebrow: '数据集工作台', exportTitle: '训练数据导出', refresh: '刷新', close: '关闭',
    allHosts: '全部 Host', allModels: '全部模型', allProtocols: '全部协议', allStatus: '全部状态', success: '成功', error: '失败',
    search: '查询', modelPh: '模型', startTime: '开始时间', endTime: '结束时间', clearTimeRange: '清除时间范围',
    invalidTimeRange: '开始时间不能晚于结束时间。', filterLoadFailed: '无法加载筛选选项',
    colTime: '时间', colProtocol: '协议', colModel: '模型', colStatus: '状态',
    colDuration: '耗时', colFirstToken: '首Token', colStream: '流式', colActions: '操作',
    prevPage: '上一页', nextPage: '下一页',
    callDetail: '调用详情', metadata: '元数据', request: '请求', response: '响应',
    headersSanitized: 'Headers (脱敏)',
    noData: '暂无数据',
    recordsSelected: '条记录已选择', selectPage: '选择当前页', selectFiltered: '选择全部筛选结果',
    clearSelection: '清空', exportSelected: '导出所选', changeSelection: '修改选择',
    selectionReady: count => `已选择 ${count} 条调用`, selectionReadyNote: '检查和导出仅处理这些明确选择的记录。',
    selectionEmpty: '尚未选择调用', selectionEmptyNote: '请先到调用列表选择要导出的记录。',
    selectFilteredConfirm: count => `选择当前筛选条件下的全部 ${count} 条记录？`,
    pageInfo: (p, t, total) => `第 ${p} 页 / 共 ${t} 页 (${total} 条)`,
    statsByHost: '按 Host 统计', statsByProtocol: '按协议统计', statsByModel: '按模型统计',
    colCount: '调用数', colAvgDuration: '平均耗时(ms)',
    totalCalls: '总调用数', successCalls: '成功调用', avgDuration: '平均耗时', avgFirstToken: '平均首Token',
    streamBadge: '流式',
    delete: '删除', deleteConfirm: '确定删除这条记录？', deleteBtn: '删除', deleted: '已删除',
    exportSettings: '导出设置', exportFormat: '数据格式',
    includeMetadata: '包含溯源元数据', includeSkipped: '包含低质量样本', includeTools: '注入工具定义',
    enableContextLimit: '限制每条记录上下文',
    contextConstraint: '单条记录上下文约束', useRecommended: '使用最大目标预算',
    maxSeqLen: '每条记录最大估算 Token', charsPerToken: '字符 / Token', prefixRatio: '固定前缀最多占比',
    exportFormatHint: '决定导出文件的结构与目标训练框架。Canonical 信息最完整；Tool SFT、OpenAI 和 ShareGPT 更适合直接训练。',
    includeMetadataHint: '在每条样本中附加来源、模型、轨迹和统计等审计字段。会增加文件体积，通常不作为训练文本。Canonical 始终包含这些信息。',
    includeSkippedHint: '同时导出被质量规则标记为应跳过的样本，例如无法形成有效训练轨迹的调用。仅建议用于排查或人工复核。',
    includeToolsHint: '在 ShareGPT 对话开头写入可用工具定义，使工具调用轨迹具备完整上下文。可能明显增加每条记录的长度。',
    contextLimitHint: '把长轨迹拆分为以 Assistant 消息为目标的窗口，并控制每条输出记录的估算长度。适合有固定上下文窗口的训练任务。',
    maxSeqLenHint: '每条输出记录允许的最大估算 Token 数。估算包含消息、工具定义和结构化工具调用，不是 tokenizer 的精确计数。',
    charsPerTokenHint: '用于把字符数换算成 Token 的估算系数。数值越小，预算越保守；中文或代码较多时可适当调低。',
    prefixRatioHint: '系统提示、工具定义等固定前缀最多占总预算的比例。超出时会裁剪前缀，为对话历史和目标回复保留空间。',
    inspectData: '检查数据', startExport: '开始导出', datasetOverview: '数据概况', generatedFiles: '最近生成',
    fileName: '文件', fileSize: '大小', createdTime: '生成时间', download: '下载',
    callsAvailable: '调用', episodesAvailable: '可解析轨迹', assistantTargets: 'Assistant 目标',
    inspecting: '正在检查数据...', exporting: '正在导出，较大的数据集可能需要一些时间...', exportDone: '导出完成', exportFailed: '导出失败',
    noExports: '暂无导出文件', recommendedBudget: '建议窗口长度',
    formatCanonical: '信息最完整的中间格式，适合审计和二次转换。',
    formatToolSft: '保留结构化 tools、tool_calls 和 tool 结果。',
    formatOpenAI: 'OpenAI messages 格式，推理会写入 <think> 块。',
    formatWindowed: '每条样本以一个 assistant 消息为训练目标，适合长轨迹。',
    formatShareGPT: 'ShareGPT 对话数组，工具轨迹使用文本标签表示。',
    contextLimitStatus: (limit, p95, maxTarget) => `每条输出记录不超过 ${limit} 估算 Token · P95 目标最低 ${p95} · 最大目标最低 ${maxTarget}`,
    actualMaxUnits: '实际最大上下文',
  },
  en: {
    title: 'Data Collection', brandProduct: 'Capture Console', captureActive: 'Capturing locally',
    tabList: 'Calls', tabStats: 'Stats', tabExport: 'Export',
    callsEyebrow: 'Capture ledger', callsTitle: 'Call records', statsEyebrow: 'Operations', statsTitle: 'Run overview',
    exportEyebrow: 'Dataset workspace', exportTitle: 'Training data export', refresh: 'Refresh', close: 'Close',
    allHosts: 'All Hosts', allModels: 'All Models', allProtocols: 'All Protocols', allStatus: 'All Status', success: 'Success', error: 'Error',
    search: 'Search', modelPh: 'Model', startTime: 'Start time', endTime: 'End time', clearTimeRange: 'Clear time range',
    invalidTimeRange: 'Start time cannot be after end time.', filterLoadFailed: 'Could not load filter options',
    colTime: 'Time', colProtocol: 'Protocol', colModel: 'Model', colStatus: 'Status',
    colDuration: 'Duration', colFirstToken: 'First Token', colStream: 'Stream', colActions: 'Actions',
    prevPage: 'Prev', nextPage: 'Next',
    callDetail: 'Call Detail', metadata: 'Metadata', request: 'Request', response: 'Response',
    headersSanitized: 'Headers (sanitized)',
    noData: 'No data',
    recordsSelected: 'records selected', selectPage: 'Select page', selectFiltered: 'Select all filtered',
    clearSelection: 'Clear', exportSelected: 'Export selected', changeSelection: 'Change selection',
    selectionReady: count => `${count} calls selected`, selectionReadyNote: 'Inspection and export process only these explicitly selected records.',
    selectionEmpty: 'No calls selected', selectionEmptyNote: 'Select the calls you want to export from the Calls view first.',
    selectFilteredConfirm: count => `Select all ${count} records matching the current filters?`,
    pageInfo: (p, t, total) => `Page ${p} of ${t} (${total} records)`,
    statsByHost: 'By Host', statsByProtocol: 'By Protocol', statsByModel: 'By Model',
    colCount: 'Count', colAvgDuration: 'Avg Duration (ms)',
    totalCalls: 'Total Calls', successCalls: 'Success', avgDuration: 'Avg Duration', avgFirstToken: 'Avg First Token',
    streamBadge: 'Stream',
    delete: 'Delete', deleteConfirm: 'Delete this record?', deleteBtn: 'Delete', deleted: 'Deleted',
    exportSettings: 'Export settings', exportFormat: 'Dataset format',
    includeMetadata: 'Include trace metadata', includeSkipped: 'Include low-quality samples', includeTools: 'Inject tool definitions',
    enableContextLimit: 'Limit context per record',
    contextConstraint: 'Per-record context constraint', useRecommended: 'Use max-target budget',
    maxSeqLen: 'Maximum estimated tokens per record', charsPerToken: 'Characters / token', prefixRatio: 'Maximum fixed-prefix ratio',
    exportFormatHint: 'Controls the output schema and target training stack. Canonical preserves the most detail; Tool SFT, OpenAI, and ShareGPT are closer to training-ready formats.',
    includeMetadataHint: 'Adds source, model, trajectory, and statistics fields for auditing. This increases file size and is normally not used as training text. Canonical always includes this information.',
    includeSkippedHint: 'Also exports samples rejected by quality rules, such as calls that cannot form a valid training trajectory. Recommended only for debugging or manual review.',
    includeToolsHint: 'Writes available tool definitions at the start of each ShareGPT conversation so tool calls have complete context. This can substantially increase record length.',
    contextLimitHint: 'Splits long trajectories into windows that target individual Assistant messages and caps the estimated length of every output record. Use this for models with a fixed context window.',
    maxSeqLenHint: 'Maximum estimated tokens allowed in each output record. The estimate includes messages, tool definitions, and structured tool calls; it is not an exact tokenizer count.',
    charsPerTokenHint: 'Approximation used to convert characters into tokens. Lower values are more conservative; consider lowering it for Chinese-heavy or code-heavy data.',
    prefixRatioHint: 'Maximum share of the budget reserved for fixed prefixes such as system prompts and tool definitions. Excess prefix content is trimmed to preserve history and the target response.',
    inspectData: 'Inspect data', startExport: 'Start export', datasetOverview: 'Dataset overview', generatedFiles: 'Recent files',
    fileName: 'File', fileSize: 'Size', createdTime: 'Created', download: 'Download',
    callsAvailable: 'Calls', episodesAvailable: 'Parsed episodes', assistantTargets: 'Assistant targets',
    inspecting: 'Inspecting data...', exporting: 'Exporting; large datasets may take some time...', exportDone: 'Export complete', exportFailed: 'Export failed',
    noExports: 'No export files yet', recommendedBudget: 'Recommended window length',
    formatCanonical: 'Highest-fidelity intermediate format for auditing and further conversion.',
    formatToolSft: 'Preserves structured tools, tool_calls, and tool results.',
    formatOpenAI: 'OpenAI messages format with reasoning emitted in <think> blocks.',
    formatWindowed: 'Each sample targets one assistant message and is suitable for long trajectories.',
    formatShareGPT: 'ShareGPT conversation array with tool trajectories represented as text tags.',
    contextLimitStatus: (limit, p95, maxTarget) => `Each output record is capped at ${limit} estimated tokens · P95 target minimum ${p95} · maximum target minimum ${maxTarget}`,
    actualMaxUnits: 'Actual largest context',
  }
};

const LANGUAGE_PREFERENCE_KEY = 'llm-tap-language-v2';
let lang = localStorage.getItem(LANGUAGE_PREFERENCE_KEY) || 'en';

function t(key) { return i18n[lang][key]; }

function setLang(l) {
  lang = l;
  localStorage.setItem(LANGUAGE_PREFERENCE_KEY, l);
  applyI18n();
  document.getElementById('lang-zh').classList.toggle('active', l === 'zh');
  document.getElementById('lang-en').classList.toggle('active', l === 'en');
  // 重新渲染动态内容
  loadCalls();
  if (document.getElementById('view-stats').style.display !== 'none') loadStats();
  if (document.getElementById('view-export').style.display !== 'none') {
    updateExportFields();
    renderExportSummary(lastExportReport);
    loadExports();
  }
  updateSelectionUi();
  loadFilterOptions();
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
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    const key = el.getAttribute('data-i18n-title');
    if (i18n[lang][key]) {
      el.title = i18n[lang][key];
      el.setAttribute('aria-label', i18n[lang][key]);
    }
  });
  document.querySelectorAll('[data-hint]').forEach(el => {
    const key = el.getAttribute('data-hint');
    const message = i18n[lang][key];
    if (message) {
      el.dataset.tooltip = message;
      el.setAttribute('aria-label', message);
    }
  });
  document.documentElement.lang = lang;
  document.title = 'llm-tap · ' + t('title');
}

function closeHints(except = null) {
  document.querySelectorAll('.hint-trigger[aria-expanded="true"]').forEach(el => {
    if (el !== except) el.setAttribute('aria-expanded', 'false');
  });
}

let currentPage = 1;
let total = 0;
let currentDetailCallId = null;
let currentPageCallIds = [];
const selectedCallIds = new Set();
const pageSize = 20;

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function setBusy(button, busy, labelKey = null) {
  if (!button) return;
  button.disabled = busy;
  button.classList.toggle('is-loading', busy);
  if (labelKey) button.textContent = t(labelKey);
}

function currentFilters() {
  const startValue = document.getElementById('f-start-time').value;
  const endValue = document.getElementById('f-end-time').value;
  return {
    host: document.getElementById('f-host').value,
    protocol: document.getElementById('f-protocol').value,
    model: document.getElementById('f-model').value,
    status: document.getElementById('f-status').value,
    start_time: startValue ? startValue + ':00' : '',
    end_time: endValue ? endValue + ':59.999999' : ''
  };
}

function applyCallFilters() {
  const startValue = document.getElementById('f-start-time').value;
  const endValue = document.getElementById('f-end-time').value;
  if (startValue && endValue && startValue > endValue) {
    alert(t('invalidTimeRange'));
    return;
  }
  currentPage = 1;
  loadCalls();
}

function clearTimeRange() {
  document.getElementById('f-start-time').value = '';
  document.getElementById('f-end-time').value = '';
  applyCallFilters();
}

function renderFilterOptions(selectId, rows, allLabelKey) {
  const select = document.getElementById(selectId);
  const currentValue = select.value;
  const options = [`<option value="">${esc(t(allLabelKey))}</option>`];
  (rows || []).forEach(row => {
    options.push(`<option value="${esc(row.value)}">${esc(row.value)} (${esc(row.count)})</option>`);
  });
  select.innerHTML = options.join('');
  if ((rows || []).some(row => row.value === currentValue)) select.value = currentValue;
}

async function loadFilterOptions() {
  try {
    const res = await fetch('/api/filter-options');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    renderFilterOptions('f-host', data.hosts, 'allHosts');
    renderFilterOptions('f-model', data.models, 'allModels');
  } catch (err) {
    console.warn(t('filterLoadFailed'), err);
  }
}

function selectedIds() {
  return Array.from(selectedCallIds);
}

function updateSelectionUi() {
  const count = selectedCallIds.size;
  document.getElementById('selected-count').textContent = count;
  document.getElementById('clear-selection-btn').disabled = count === 0;
  document.getElementById('export-selected-btn').disabled = count === 0;

  const scope = document.getElementById('selection-scope');
  scope.classList.toggle('is-empty', count === 0);
  document.getElementById('selection-scope-title').textContent = count ? t('selectionReady')(count) : t('selectionEmpty');
  document.getElementById('selection-scope-note').textContent = count ? t('selectionReadyNote') : t('selectionEmptyNote');
  document.getElementById('inspect-btn').disabled = count === 0;
  document.getElementById('export-btn').disabled = count === 0;

  document.querySelectorAll('.call-select').forEach(input => {
    input.checked = selectedCallIds.has(input.dataset.callId);
    const row = input.closest('tr');
    if (row) row.classList.toggle('is-selected', input.checked);
  });
  const pageCheckbox = document.getElementById('select-page-checkbox');
  const selectedOnPage = currentPageCallIds.filter(id => selectedCallIds.has(id)).length;
  pageCheckbox.checked = currentPageCallIds.length > 0 && selectedOnPage === currentPageCallIds.length;
  pageCheckbox.indeterminate = selectedOnPage > 0 && selectedOnPage < currentPageCallIds.length;
}

function toggleCallSelection(event, callId) {
  event.stopPropagation();
  if (event.currentTarget.checked) selectedCallIds.add(callId);
  else selectedCallIds.delete(callId);
  lastExportReport = null;
  renderExportSummary(null);
  updateSelectionUi();
}

function toggleCurrentPage(checked) {
  currentPageCallIds.forEach(callId => checked ? selectedCallIds.add(callId) : selectedCallIds.delete(callId));
  lastExportReport = null;
  renderExportSummary(null);
  updateSelectionUi();
}

function selectCurrentPage() {
  toggleCurrentPage(true);
}

async function selectFilteredCalls() {
  const button = document.getElementById('select-filtered-btn');
  setBusy(button, true);
  try {
    const params = new URLSearchParams(currentFilters());
    const res = await fetch('/api/call-ids?' + params);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    if (!confirm(t('selectFilteredConfirm')(data.total || 0))) return;
    (data.call_ids || []).forEach(callId => selectedCallIds.add(callId));
    lastExportReport = null;
    renderExportSummary(null);
    updateSelectionUi();
  } catch (err) {
    alert((err && err.message) ? err.message : String(err));
  } finally {
    setBusy(button, false);
  }
}

function clearSelection() {
  selectedCallIds.clear();
  lastExportReport = null;
  renderExportSummary(null);
  clearExportStatus();
  updateSelectionUi();
}

function openSelectedExport() {
  if (!selectedCallIds.size) return;
  switchTab('export');
}

async function loadCalls() {
  const refreshButton = document.getElementById('calls-refresh');
  setBusy(refreshButton, true);
  const params = new URLSearchParams({page: currentPage, page_size: pageSize, ...currentFilters()});
  const tbody = document.getElementById('calls-tbody');
  try {
    const res = await fetch('/api/calls?' + params);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    total = data.total;
    currentPageCallIds = data.data.map(row => row.call_id);
    tbody.innerHTML = data.data.map(r => `
      <tr onclick="showDetail('${esc(r.call_id)}')">
        <td class="select-cell"><input type="checkbox" class="call-select" data-call-id="${esc(r.call_id)}" onclick="event.stopPropagation()" onchange="toggleCallSelection(event, '${esc(r.call_id)}')" aria-label="${esc(t('exportSelected'))}"></td>
        <td>${esc(fmtTime(r.started_at))}</td>
        <td>${esc(r.upstream_provider || '-')}</td>
        <td><span class="tag">${esc(r.protocol)}</span></td>
        <td>${esc(r.upstream_model || '-')}</td>
        <td>${r.upstream_status === 200 ? '<span class="badge badge-success">200</span>' : '<span class="badge badge-error">'+esc(r.upstream_status)+'</span>'}</td>
        <td>${r.duration_ms ? esc(r.duration_ms)+'ms' : '-'}</td>
        <td>${r.first_token_ms ? esc(r.first_token_ms)+'ms' : '-'}</td>
        <td>${r.is_stream ? '<span class="badge badge-stream">'+t('streamBadge')+'</span>' : '-'}</td>
        <td class="action-cell"><button class="danger-btn" onclick="deleteCall(event, '${esc(r.call_id)}')">${t('delete')}</button></td>
      </tr>
    `).join('') || '<tr><td colspan="10" style="text-align:center;color:#87939a;padding:40px">'+t('noData')+'</td></tr>';
    const totalPages = Math.max(1, Math.ceil(total/pageSize));
    document.getElementById('page-info').textContent = t('pageInfo')(currentPage, totalPages, total);
    document.getElementById('btn-prev').disabled = currentPage <= 1;
    document.getElementById('btn-next').disabled = currentPage >= totalPages;
    updateSelectionUi();
  } catch (err) {
    currentPageCallIds = [];
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#a33;padding:40px">'+esc((err && err.message) ? err.message : String(err))+'</td></tr>';
  } finally {
    setBusy(refreshButton, false);
  }
}

async function refreshCallsView() {
  await Promise.all([loadFilterOptions(), loadCalls()]);
}

function changePage(d) {
  currentPage += d;
  if (currentPage < 1) currentPage = 1;
  loadCalls();
}

async function deleteCall(event, callId, closeAfterDelete = false) {
  event.stopPropagation();
  if (!callId) return;
  if (!confirm(t('deleteConfirm') + '\\n' + callId)) return;
  const btn = event.currentTarget;
  btn.disabled = true;
  try {
    const res = await fetch('/api/calls/' + encodeURIComponent(callId), { method: 'DELETE' });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || ('HTTP ' + res.status));
    }
    total = Math.max(0, total - 1);
    selectedCallIds.delete(callId);
    lastExportReport = null;
    currentPage = Math.min(currentPage, Math.max(1, Math.ceil(total / pageSize)));
    if (closeAfterDelete) closeModal();
    await loadCalls();
    if (document.getElementById('view-stats').style.display !== 'none') loadStats();
  } catch (err) {
    alert((err && err.message) ? err.message : String(err));
    btn.disabled = false;
  }
}

async function showDetail(callId) {
  currentDetailCallId = callId;
  document.getElementById('modal-delete-btn').disabled = false;
  const res = await fetch('/api/calls/' + callId);
  const data = await res.json();
  const call = data.call || {};
  const meta = call.meta || data.meta || {};
  document.getElementById('modal-title').textContent = t('callDetail');
  // Escape HTML so JSON content containing <script>/<b>/etc is shown as text,
  // not rendered as HTML. Without this, markdown/HTML inside response bodies
  // breaks the <pre> block and messes up the modal layout.
  function preBlock(obj) { return '<pre>' + esc(JSON.stringify(obj, null, 2)) + '</pre>'; }
  function detailBlock(title, obj, open) {
    return '<details class="detail-section"'+(open ? ' open' : '')+'><summary>'+title+'</summary>' + preBlock(obj) + '</details>';
  }
  let html = '';
  html += detailBlock(t('metadata'), meta, false);
  if (call.request) html += detailBlock(t('request'), call.request, false);
  if (call.response) html += detailBlock(t('response'), call.response, true);
  if (call.headers) html += detailBlock(t('headersSanitized'), call.headers, false);
  document.getElementById('modal-body').innerHTML = html;
  document.getElementById('modal').classList.add('show');
  document.getElementById('modal').setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
}

function closeModal() {
  currentDetailCallId = null;
  document.getElementById('modal').classList.remove('show');
  document.getElementById('modal').setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
}

async function loadStats() {
  const refreshButton = document.getElementById('stats-refresh');
  setBusy(refreshButton, true);
  try {
    const res = await fetch('/api/stats');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    const ov = data.overview || {};
    document.getElementById('stats-overview').innerHTML = `
      <div class="stat-card"><div class="label">${t('totalCalls')}</div><div class="value">${esc(ov.total||0)}</div></div>
      <div class="stat-card"><div class="label">${t('successCalls')}</div><div class="value metric-success">${esc(ov.success||0)}</div></div>
      <div class="stat-card"><div class="label">${t('avgDuration')}</div><div class="value">${esc(Math.round(ov.avg_duration||0))}<span>ms</span></div></div>
      <div class="stat-card"><div class="label">${t('avgFirstToken')}</div><div class="value">${esc(Math.round(ov.avg_first_token||0))}<span>ms</span></div></div>
    `;
    document.getElementById('stats-host').innerHTML = (data.by_host||[]).map(r =>
      `<tr><td>${esc(r.host||'-')}</td><td>${esc(r.count)}</td><td class="metric-success">${esc(r.success||0)}</td><td>${esc(Math.round(r.avg_duration||0))}</td></tr>`
    ).join('') || '<tr><td colspan="4" style="text-align:center;color:#87939a">'+t('noData')+'</td></tr>';
    document.getElementById('stats-protocol').innerHTML = (data.by_protocol||[]).map(r =>
      `<tr><td><span class="tag">${esc(r.protocol)}</span></td><td>${esc(r.count)}</td></tr>`
    ).join('') || '<tr><td colspan="2" style="text-align:center;color:#87939a">'+t('noData')+'</td></tr>';
    document.getElementById('stats-model').innerHTML = (data.by_model||[]).map(r =>
      `<tr><td>${esc(r.model||'-')}</td><td>${esc(r.count)}</td></tr>`
    ).join('') || '<tr><td colspan="2" style="text-align:center;color:#87939a">'+t('noData')+'</td></tr>';
  } finally {
    setBusy(refreshButton, false);
  }
}

const formatDescriptionKeys = {
  canonical: 'formatCanonical',
  tool_sft: 'formatToolSft',
  openai: 'formatOpenAI',
  sharegpt: 'formatShareGPT'
};
let lastExportReport = null;

function updateExportFields() {
  const format = document.getElementById('export-format').value;
  const contextLimited = document.getElementById('export-context-limit').checked;
  document.getElementById('sharegpt-tools-row').style.display = format === 'sharegpt' ? 'flex' : 'none';
  document.getElementById('window-options').style.display = contextLimited ? '' : 'none';
  document.getElementById('export-metadata').disabled = format === 'canonical';
  document.getElementById('format-description').textContent = t(formatDescriptionKeys[format]);
  updateContextConstraintStatus();
}

function updateContextConstraintStatus() {
  const budget = (lastExportReport && lastExportReport.window_budget) || {};
  const limit = Number(document.getElementById('export-max-seq').value || 4096);
  const p95 = budget.p95_min_max_seq_len || '-';
  const maxTarget = budget.recommended_min_max_seq_len || '-';
  document.getElementById('context-limit-status').textContent = t('contextLimitStatus')(limit, p95, maxTarget);
  document.getElementById('use-recommended-btn').disabled = !budget.recommended_min_max_seq_len;
}

function useRecommendedBudget() {
  const budget = (lastExportReport && lastExportReport.window_budget) || {};
  if (!budget.recommended_min_max_seq_len) return;
  document.getElementById('export-max-seq').value = budget.recommended_min_max_seq_len;
  updateContextConstraintStatus();
}

function setExportStatus(message, kind = 'info') {
  const box = document.getElementById('export-status');
  box.className = 'status-box show status-' + kind;
  box.textContent = message;
}

function clearExportStatus() {
  const box = document.getElementById('export-status');
  box.className = 'status-box';
  box.textContent = '';
}

function fmtBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return value + ' B';
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + ' KB';
  if (value < 1024 * 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + ' MB';
  return (value / 1024 / 1024 / 1024).toFixed(1) + ' GB';
}

function renderExportSummary(report) {
  const summary = document.getElementById('export-summary');
  if (!report) {
    summary.innerHTML = '<div class="summary-item"><span>'+t('callsAvailable')+'</span><strong>-</strong></div>' +
      '<div class="summary-item"><span>'+t('episodesAvailable')+'</span><strong>-</strong></div>' +
      '<div class="summary-item"><span>'+t('assistantTargets')+'</span><strong>-</strong></div>';
    document.getElementById('window-budget-note').textContent = '';
    return;
  }
  const budget = report.window_budget || {};
  summary.innerHTML = `
    <div class="summary-item"><span>${t('callsAvailable')}</span><strong>${report.calls || 0}</strong></div>
    <div class="summary-item"><span>${t('episodesAvailable')}</span><strong>${report.episodes || 0}</strong></div>
    <div class="summary-item"><span>${t('assistantTargets')}</span><strong>${budget.assistant_targets || '-'}</strong></div>
  `;
  document.getElementById('window-budget-note').textContent = budget.recommended_min_max_seq_len
    ? t('recommendedBudget') + ': ' + budget.recommended_min_max_seq_len +
      ' · P95: ' + (budget.p95_min_max_seq_len || '-') + ' · ' + (budget.chars_per_token || 4) + ' chars/token'
    : '';
  updateContextConstraintStatus();
}

async function loadExportInspect(showStatus = false) {
  const inspectBtn = document.getElementById('inspect-btn');
  if (!selectedCallIds.size) {
    updateSelectionUi();
    return;
  }
  setBusy(inspectBtn, true);
  if (showStatus) setExportStatus(t('inspecting'), 'info');
  try {
    const res = await fetch('/api/export/inspect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        call_ids: selectedIds(),
        include_window_budget: true,
        chars_per_token: Number(document.getElementById('export-chars-token').value || 4.0)
      })
    });
    const report = await res.json();
    if (!res.ok) throw new Error(report.error || ('HTTP ' + res.status));
    lastExportReport = report;
    renderExportSummary(report);
    if (showStatus) clearExportStatus();
  } catch (err) {
    setExportStatus((err && err.message) ? err.message : String(err), 'error');
  } finally {
    setBusy(inspectBtn, false);
    updateSelectionUi();
  }
}

async function createExport() {
  const button = document.getElementById('export-btn');
  if (!selectedCallIds.size) {
    updateSelectionUi();
    return;
  }
  setBusy(button, true);
  setExportStatus(t('exporting'), 'info');
  const payload = {
    format: document.getElementById('export-format').value,
    call_ids: selectedIds(),
    include_metadata: document.getElementById('export-metadata').checked,
    include_skipped: document.getElementById('export-skipped').checked,
    include_tools: document.getElementById('export-tools').checked,
    context_limit: document.getElementById('export-context-limit').checked,
    max_seq_len: Number(document.getElementById('export-max-seq').value),
    chars_per_token: Number(document.getElementById('export-chars-token').value),
    prefix_budget_ratio: Number(document.getElementById('export-prefix-ratio').value)
  };
  try {
    const res = await fetch('/api/export', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const result = await res.json();
    if (!res.ok) throw new Error(result.error || ('HTTP ' + res.status));
    const written = result.written == null ? 0 : result.written;
    const link = result.download_url;
    const box = document.getElementById('export-status');
    box.className = 'status-box show status-success';
    const maxUnits = result.context_limited && result.max_estimated_units != null
      ? ` · ${t('actualMaxUnits')}: ${result.max_estimated_units}/${result.max_seq_len}`
      : '';
    box.innerHTML = `${t('exportDone')}: ${written} · ${fmtBytes(result.size_bytes)}${maxUnits} · <a class="download-link" href="${link}">${t('download')}</a>`;
    await loadExports();
  } catch (err) {
    setExportStatus(t('exportFailed') + ': ' + ((err && err.message) ? err.message : String(err)), 'error');
  } finally {
    setBusy(button, false);
    updateSelectionUi();
  }
}

async function loadExports() {
  const tbody = document.getElementById('exports-tbody');
  try {
    const res = await fetch('/api/exports');
    const result = await res.json();
    if (!res.ok) throw new Error(result.error || ('HTTP ' + res.status));
    tbody.innerHTML = (result.data || []).map(row => `
      <tr>
        <td title="${esc(row.filename)}">${esc(row.filename)}</td>
        <td>${esc(fmtBytes(row.size_bytes))}</td>
        <td>${esc(fmtTime(row.created_at))}</td>
        <td><a class="download-link" href="${esc(row.download_url)}">${t('download')}</a></td>
      </tr>
    `).join('') || '<tr><td colspan="4" style="text-align:center;color:#87939a;padding:30px">'+t('noExports')+'</td></tr>';
  } catch (err) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#a33;padding:30px">'+
      esc((err && err.message) ? err.message : String(err))+'</td></tr>';
  }
}

async function refreshExportView() {
  const button = document.getElementById('export-refresh');
  setBusy(button, true);
  try {
    const tasks = [loadExports()];
    if (selectedCallIds.size) tasks.push(loadExportInspect(false));
    await Promise.all(tasks);
  } finally {
    setBusy(button, false);
  }
}

function switchTab(tab) {
  document.getElementById('view-list').style.display = tab === 'list' ? '' : 'none';
  document.getElementById('view-stats').style.display = tab === 'stats' ? '' : 'none';
  document.getElementById('view-export').style.display = tab === 'export' ? '' : 'none';
  document.getElementById('tab-list').classList.toggle('active', tab === 'list');
  document.getElementById('tab-stats').classList.toggle('active', tab === 'stats');
  document.getElementById('tab-export').classList.toggle('active', tab === 'export');
  document.getElementById('tab-list').setAttribute('aria-current', tab === 'list' ? 'page' : 'false');
  document.getElementById('tab-stats').setAttribute('aria-current', tab === 'stats' ? 'page' : 'false');
  document.getElementById('tab-export').setAttribute('aria-current', tab === 'export' ? 'page' : 'false');
  sessionStorage.setItem('activeTab', tab);
  if (tab === 'stats') loadStats();
  if (tab === 'export') {
    updateExportFields();
    updateSelectionUi();
    if (selectedCallIds.size && !lastExportReport) loadExportInspect(false);
    loadExports();
  }
}

function fmtTime(s) {
  if (!s) return '-';
  return s.replace('T', ' ').substring(0, 19);
}

// 初始化
setLang(lang);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    closeHints();
    if (document.getElementById('modal').classList.contains('show')) closeModal();
  }
});
document.addEventListener('click', event => {
  const trigger = event.target.closest('.hint-trigger');
  if (trigger) {
    const willOpen = trigger.getAttribute('aria-expanded') !== 'true';
    closeHints(trigger);
    trigger.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    event.stopPropagation();
    return;
  }
  closeHints();
});
switchTab(sessionStorage.getItem('activeTab') || 'list');
</script>
</body>
</html>"""


# ========== 主函数 ==========

def _register_routes(server: "ProxyServer") -> None:
    """Register frontend + proxy routes on a server instance."""
    server.app.router.add_get("/", server.handle_index)
    server.app.router.add_get("/api/calls", server.handle_api_calls)
    server.app.router.add_get("/api/filter-options", server.handle_api_filter_options)
    server.app.router.add_get("/api/call-ids", server.handle_api_call_ids)
    server.app.router.add_get("/api/calls/{call_id}", server.handle_api_call_detail)
    server.app.router.add_delete("/api/calls/{call_id}", server.handle_api_call_delete)
    server.app.router.add_get("/api/stats", server.handle_api_stats)
    server.app.router.add_post("/api/export/inspect", server.handle_api_export_inspect)
    server.app.router.add_post("/api/export", server.handle_api_export)
    server.app.router.add_get("/api/exports", server.handle_api_exports)
    server.app.router.add_get("/api/exports/{filename}", server.handle_api_export_download)
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
