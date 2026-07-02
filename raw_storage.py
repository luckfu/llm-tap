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
import logging
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


async def _configure_db_connection(conn: aiosqlite.Connection) -> None:
    """Tune SQLite for concurrent readers plus one background writer."""
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA temp_store=MEMORY")


async def init_calls_table(db_path: str = "calls.db") -> None:
    """初始化 calls 表"""
    async with aiosqlite.connect(db_path) as conn:
        await _configure_db_connection(conn)
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

def _write_json_sync(path: str, obj: Any, indent: Optional[int] = 2) -> None:
    with open(path, "w", encoding="utf-8") as f:
        if indent is None:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=indent)


async def _write_json_async(path: str, obj: Any, indent: Optional[int] = 2) -> None:
    await asyncio.to_thread(_write_json_sync, path, obj, indent)


def _prepare_call_payload(
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
) -> tuple[str, Dict[str, Any], Dict[str, Any], str]:
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

    file_path = _build_path(call_id, started_at, upstream_provider)
    tags_json = json.dumps(tags or [], ensure_ascii=False)
    return file_path, call_record, meta, tags_json


async def _insert_call_row(
    conn: aiosqlite.Connection,
    meta: Dict[str, Any],
    file_path: str,
    tags_json: str,
) -> None:
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
            meta["call_id"], meta.get("parent_call_id"), meta.get("session_id"), meta.get("agent_id"),
            meta["protocol"], meta.get("request_path"),
            meta.get("upstream_provider"), meta.get("upstream_model"), meta.get("client_model_alias"),
            meta.get("started_at"), meta.get("finished_at"),
            meta.get("duration_ms"), meta.get("first_token_ms"),
            meta.get("upstream_status"), meta.get("stop_reason"), meta.get("upstream_error"),
            file_path,
            1 if meta.get("is_stream") else 0,
            tags_json,
        ),
    )


def _notify_call_saved(meta: Dict[str, Any]) -> None:
    # 通知订阅者（托盘等外部组件）
    for cb in _call_saved_callbacks:
        try:
            cb(meta)
        except Exception as e:
            logging.getLogger("raw_storage").error(
                f"call-saved callback {cb!r} raised: {e!r}"
            )


async def _save_raw_call_with_conn(
    conn: aiosqlite.Connection,
    *,
    json_indent: Optional[int] = 2,
    **kwargs,
) -> tuple[str, Dict[str, Any]]:
    file_path, call_record, meta, tags_json = _prepare_call_payload(**kwargs)
    await _write_json_async(file_path, call_record, indent=json_indent)
    await _insert_call_row(conn, meta, file_path, tags_json)
    return file_path, meta


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
    """
    async with aiosqlite.connect(db_path) as conn:
        await _configure_db_connection(conn)
        file_path, meta = await _save_raw_call_with_conn(
            conn,
            json_indent=2,
            call_id=call_id,
            protocol=protocol,
            request_path=request_path,
            request_body=request_body,
            request_headers=request_headers,
            upstream_provider=upstream_provider,
            upstream_model=upstream_model,
            client_model_alias=client_model_alias,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            first_token_ms=first_token_ms,
            upstream_status=upstream_status,
            stop_reason=stop_reason,
            upstream_error=upstream_error,
            response_body=response_body,
            is_stream=is_stream,
            parent_call_id=parent_call_id,
            session_id=session_id,
            agent_id=agent_id,
            tags=tags,
        )
        await conn.commit()
        _notify_call_saved(meta)
        return file_path


class RawCallWriter:
    """Background single-writer queue for capture persistence."""

    def __init__(
        self,
        db_path: str = "calls.db",
        max_queue: int = 1000,
        *,
        batch_size: int = 20,
        json_indent: Optional[int] = None,
    ):
        self.db_path = db_path
        self.max_queue = max_queue
        self.batch_size = max(1, batch_size)
        self.json_indent = json_indent
        self.queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue(maxsize=max_queue)
        self.task: Optional[asyncio.Task] = None
        self.conn: Optional[aiosqlite.Connection] = None
        self.closed = False
        self.dropped = 0
        self.logger = logging.getLogger("raw_storage")

    async def start(self) -> None:
        if self.task is not None:
            return
        self.conn = await aiosqlite.connect(self.db_path)
        await _configure_db_connection(self.conn)
        await self.conn.execute(CALLS_TABLE_DDL)
        for ddl in CALLS_INDEXES:
            await self.conn.execute(ddl)
        await self.conn.commit()
        self.task = asyncio.create_task(self._run(), name="raw-call-writer")

    def enqueue(self, **kwargs) -> bool:
        if self.closed or self.task is None:
            return False
        try:
            self.queue.put_nowait(kwargs)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    async def _run(self) -> None:
        assert self.conn is not None
        while True:
            item = await self.queue.get()
            batch = []
            stop_after_batch = False
            try:
                if item is None:
                    self.queue.task_done()
                    return
                batch.append(item)
                while len(batch) < self.batch_size:
                    try:
                        next_item = self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if next_item is None:
                        stop_after_batch = True
                        self.queue.task_done()
                        break
                    batch.append(next_item)

                saved_meta = []
                try:
                    for queued in batch:
                        _, meta = await _save_raw_call_with_conn(
                            self.conn,
                            json_indent=self.json_indent,
                            **queued,
                        )
                        saved_meta.append(meta)
                    await self.conn.commit()
                    for meta in saved_meta:
                        _notify_call_saved(meta)
                except Exception as e:
                    await self.conn.rollback()
                    self.logger.error(f"failed to persist raw call batch: {e}", exc_info=True)
                if stop_after_batch:
                    return
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def stop(self, *, drain: bool = True) -> None:
        self.closed = True
        if self.task is not None:
            if drain:
                await self.queue.join()
            await self.queue.put(None)
            await self.task
            self.task = None
        if self.conn is not None:
            await self.conn.close()
            self.conn = None


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
