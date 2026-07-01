"""
原始调用保真存储模块

职责：
1. 原样保存一次完整调用（请求 + 响应 + 元数据）到单个 JSON 文件
2. 在 calls 表中记录元数据 + 文件路径
3. 不做任何协议转换、不生成 ShareGPT 等训练格式

存储布局：
    data/
    └── calls/
        └── 2026/06/30/
            └── {call_id}.json   # 一次完整调用（问+答+元数据）

单个文件结构：
    {
        "meta": {
            "call_id": "...",
            "protocol": "anthropic-messages",
            "upstream_provider": "...",
            "upstream_model": "...",
            ...
        },
        "request": { ... },   # 该协议原样请求体
        "response": { ... },  # 流式整合后的完整响应（等价非流式）
        "headers": { ... }    # 脱敏后的 headers
    }
"""

import os
import json
import asyncio
import aiosqlite
from datetime import datetime
from typing import Optional, Dict, Any, List, Iterable, Callable


# ========== 调用保存事件钩子（供托盘等外部组件订阅） ==========

_call_saved_callbacks: List[Callable[[Dict[str, Any]], None]] = []


def register_call_saved_callback(fn: Callable[[Dict[str, Any]], None]) -> None:
    """Register a callback(meta: dict) fired after each successful save.

    Callbacks run in the proxy's asyncio loop thread; they must be non-blocking
    and thread-safe (e.g., updating a tray icon via pystray).
    """
    _call_saved_callbacks.append(fn)


# ========== 数据库 ==========

CALLS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS calls (
    call_id              TEXT PRIMARY KEY,
    parent_call_id       TEXT,
    session_id           TEXT,
    agent_id             TEXT,
    protocol             TEXT NOT NULL,
    request_path         TEXT,
    upstream_provider    TEXT,
    upstream_model       TEXT,
    client_model_alias   TEXT,
    started_at           TEXT,
    finished_at          TEXT,
    duration_ms          INTEGER,
    first_token_ms       INTEGER,
    upstream_status      INTEGER,
    stop_reason          TEXT,
    upstream_error       TEXT,
    raw_path             TEXT,
    is_stream            INTEGER,
    retry_count          INTEGER DEFAULT 0,
    tags                 TEXT
);
"""

CALLS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_calls_protocol ON calls(protocol);",
    "CREATE INDEX IF NOT EXISTS idx_calls_session ON calls(session_id);",
    "CREATE INDEX IF NOT EXISTS idx_calls_parent ON calls(parent_call_id);",
    "CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at);",
]


async def init_calls_table(db_path: str = "calls.db") -> None:
    """初始化 calls 表"""
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(CALLS_TABLE_DDL)
        for ddl in CALLS_INDEXES:
            await conn.execute(ddl)
        await conn.commit()


# ========== 文件路径 ==========

DATA_ROOT = "data/calls"


def _build_path(call_id: str, started_at: datetime, upstream_provider: str = "unknown") -> str:
    """为一次 call 构建文件路径，按 provider 分目录 + 日期分目录"""
    provider_dir = upstream_provider or "unknown"
    day_dir = os.path.join(DATA_ROOT, provider_dir, started_at.strftime("%Y/%m/%d"))
    os.makedirs(day_dir, exist_ok=True)
    return os.path.join(day_dir, f"{call_id}.json")


# ========== Headers 脱敏 ==========

SENSITIVE_HEADER_KEYS = {
    "authorization", "x-api-key", "x-goog-api-key",
    "anthropic-api-key", "anthropic-authorization",
    "proxy-authorization", "cookie", "set-cookie",
}


def _sanitize_headers(headers: Iterable) -> Dict[str, str]:
    """脱敏 headers：敏感字段只保留长度信息，不保留原值"""
    sanitized = {}
    if hasattr(headers, "items"):
        items = headers.items()
    else:
        items = headers
    for k, v in items:
        lower = k.lower()
        if lower in SENSITIVE_HEADER_KEYS:
            sanitized[k] = f"<redacted:len={len(str(v))}>"
        else:
            sanitized[k] = v
    return sanitized


# ========== 文件写入（异步，使用线程池避免阻塞事件循环） ==========

def _write_json_sync(path: str, obj: Any, indent: int = 2) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)


async def _write_json_async(path: str, obj: Any, indent: int = 2) -> None:
    await asyncio.to_thread(_write_json_sync, path, obj, indent)


# ========== 主入口：保存一次原始调用 ==========


async def save_raw_call(
    db_path: str,
    *,
    call_id: str,
    protocol: str,
    request_path: str,
    request_body: bytes,
    request_headers: Iterable,
    upstream_provider: str,
    upstream_model: str,
    client_model_alias: str,
    started_at: datetime,
    finished_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    first_token_ms: Optional[int] = None,
    upstream_status: Optional[int] = None,
    stop_reason: Optional[str] = None,
    upstream_error: Optional[str] = None,
    response_body: Optional[dict] = None,
    is_stream: bool = False,
    parent_call_id: Optional[str] = None,
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> str:
    """
    原样保存一次完整调用（请求+响应+元数据）到单个 JSON 文件，并写入 calls 表。

    参数：
        request_body: 原始请求体字节（保持协议原结构，由调用方传入未修改的版本）
        response_body: 整合后的完整响应对象（流式时由 stream_merger 产出，
                       非流式时直接是上游返回的 JSON）。如果上游报错可为 None。
        request_headers: 原始请求头（可迭代对象），函数内部会脱敏。
    返回：
        保存的文件路径
    """
    if finished_at is None:
        finished_at = datetime.now()
    if duration_ms is None:
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)

    # 解析原始请求体
    try:
        req_obj = json.loads(request_body.decode("utf-8")) if request_body else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        req_obj = request_body.decode("utf-8", errors="replace") if request_body else None

    # 脱敏 headers
    sanitized_headers = _sanitize_headers(request_headers)

    # 元数据
    meta = {
        "call_id": call_id,
        "parent_call_id": parent_call_id,
        "session_id": session_id,
        "agent_id": agent_id,
        "protocol": protocol,
        "request_path": request_path,
        "upstream_provider": upstream_provider,
        "upstream_model": upstream_model,
        "client_model_alias": client_model_alias,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": duration_ms,
        "first_token_ms": first_token_ms,
        "upstream_status": upstream_status,
        "stop_reason": stop_reason,
        "upstream_error": upstream_error,
        "is_stream": is_stream,
        "tags": tags or [],
    }

    # 组装完整调用记录（一个文件包含问+答+元数据）
    call_record = {
        "meta": meta,
        "request": req_obj,
        "response": response_body,
        "headers": sanitized_headers,
    }

    # 写文件
    file_path = _build_path(call_id, started_at, upstream_provider)
    await _write_json_async(file_path, call_record)

    # 写 DB 元数据 + 文件路径
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            INSERT INTO calls (
                call_id, parent_call_id, session_id, agent_id,
                protocol, request_path,
                upstream_provider, upstream_model, client_model_alias,
                started_at, finished_at, duration_ms, first_token_ms,
                upstream_status, stop_reason, upstream_error,
                raw_path, is_stream, tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id, parent_call_id, session_id, agent_id,
                protocol, request_path,
                upstream_provider, upstream_model, client_model_alias,
                started_at.isoformat(), finished_at.isoformat(),
                duration_ms, first_token_ms,
                upstream_status, stop_reason, upstream_error,
                file_path,
                1 if is_stream else 0,
                json.dumps(tags or [], ensure_ascii=False),
            ),
        )
        await conn.commit()

    # 通知订阅者（托盘等外部组件）
    for cb in _call_saved_callbacks:
        try:
            cb(meta)
        except Exception:
            pass

    return file_path


def extract_agent_metadata(headers: Iterable) -> Dict[str, Optional[str]]:
    """
    从请求 headers 中提取 agent 协作相关的元数据。
    约定 headers（不区分大小写）：
        X-Session-Id      - 客户端会话 ID
        X-Agent-Id        - 发起方 agent 标识
        X-Parent-Call-Id  - 父调用 ID（多 agent 链路追溯）
    """
    result = {"session_id": None, "agent_id": None, "parent_call_id": None}
    if hasattr(headers, "items"):
        items = headers.items()
    else:
        items = headers
    for k, v in items:
        lower = k.lower()
        if lower == "x-session-id" and v:
            result["session_id"] = v
        elif lower == "x-agent-id" and v:
            result["agent_id"] = v
        elif lower == "x-parent-call-id" and v:
            result["parent_call_id"] = v
    return result
