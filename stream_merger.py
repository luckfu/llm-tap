"""
流式响应整合模块

职责：
将 SSE 流式 chunk 序列整合成"等价于非流式调用的完整响应对象"。

原则：
- 每个协议一个 Merger 类，产出该协议的原生结构（非流式版）
- 不做协议转换（Anthropic 不会变成 OpenAI）
- 保留所有协议字段（包括 thinking signature、usage 子字段等）

支持的协议：
- openai-chat: /v1/chat/completions
- anthropic-messages: /v1/messages
"""

import json
from typing import Optional, Dict, Any, List


# ========== 协议识别 ==========


def detect_protocol(request_path: str) -> str:
    """根据请求路径识别协议"""
    path = request_path.lower()
    # 注意 /v1/messages 不能匹配到 /v1/messages-helper 之类（这里只前缀匹配）
    if path.endswith("/v1/messages") or "/v1/messages?" in path or path.endswith("/anthropic/v1/messages"):
        return "anthropic-messages"
    if "/v1/responses" in path:
        return "openai-responses"
    if "/v1/chat/completions" in path or path.endswith("/chat/completions"):
        return "openai-chat"
    return "unknown"


# ========== SSE 行解析 ==========


def parse_sse_line(line: str) -> Optional[dict]:
    """
    解析一行 SSE 数据，返回事件 dict。
    - 注释行（以 : 开头）返回 None
    - event: 行返回 None（事件类型已经在 data 的 type 字段里）
    - data: 行尝试 JSON 解析；[DONE] 返回 {"_done": True}
    - 其他行返回 None
    """
    line = line.rstrip("\r\n")
    if not line:
        return None
    if line.startswith(":"):
        return None
    if line.startswith("event:"):
        return None
    if line.startswith("data:"):
        data = line[5:].lstrip()
        if data == "[DONE]":
            return {"_done": True}
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None
    return None


def parse_sse_lines(lines: List[str]) -> List[dict]:
    """解析多行 SSE，返回事件列表（已过滤 None）"""
    events = []
    for line in lines:
        evt = parse_sse_line(line)
        if evt is not None:
            events.append(evt)
    return events


# ========== OpenAI Chat Completions Merger ==========


class OpenAIChatMerger:
    """
    将 OpenAI Chat Completions 的流式 chunk 序列合并成完整的 chat.completion 对象。

    产出等价于 stream=false 调用的响应：
        {
            "id": ...,
            "object": "chat.completion",
            "created": ...,
            "model": ...,
            "system_fingerprint": ...,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": ..., "reasoning_content": ..., "tool_calls": [...]},
                "finish_reason": ...
            }],
            "usage": ...
        }
    """

    def __init__(self):
        self.response_id: Optional[str] = None
        self.model: Optional[str] = None
        self.created: Optional[int] = None
        self.system_fingerprint: Optional[str] = None
        self.role: str = "assistant"
        self.content_parts: List[str] = []
        self.reasoning_parts: List[str] = []
        self.tool_calls: Dict[int, Dict[str, Any]] = {}
        self.finish_reason: Optional[str] = None
        self.usage: Optional[Dict[str, Any]] = None

    def feed(self, chunk: dict) -> None:
        if not isinstance(chunk, dict):
            return

        if self.response_id is None and "id" in chunk:
            self.response_id = chunk.get("id")
        if self.model is None and "model" in chunk:
            self.model = chunk.get("model")
        if self.created is None and "created" in chunk:
            self.created = chunk.get("created")
        if self.system_fingerprint is None and "system_fingerprint" in chunk:
            self.system_fingerprint = chunk.get("system_fingerprint")

        choices = chunk.get("choices") or []
        if not choices:
            # 只有 usage 字段的 chunk（部分服务商会单独发一个 usage chunk）
            if "usage" in chunk and chunk["usage"]:
                self.usage = chunk["usage"]
            return

        choice = choices[0]
        delta = choice.get("delta", {}) or {}

        if "role" in delta and delta["role"]:
            self.role = delta["role"]

        content = delta.get("content")
        if content:
            self.content_parts.append(content)

        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            self.reasoning_parts.append(reasoning)

        tool_calls_delta = delta.get("tool_calls")
        if tool_calls_delta:
            for tc in tool_calls_delta:
                idx = tc.get("index", 0)
                if idx not in self.tool_calls:
                    self.tool_calls[idx] = {
                        "id": None,
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                cur = self.tool_calls[idx]
                if tc.get("id"):
                    cur["id"] = tc["id"]
                if tc.get("type"):
                    cur["type"] = tc["type"]
                fn = tc.get("function", {}) or {}
                if fn.get("name"):
                    cur["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    cur["function"]["arguments"] += fn["arguments"]

        fr = choice.get("finish_reason")
        if fr:
            self.finish_reason = fr

        if "usage" in chunk and chunk["usage"]:
            self.usage = chunk["usage"]

    def feed_raw_line(self, line: str) -> Optional[dict]:
        """喂入原始 SSE 行，返回解析出的事件（None 表示忽略）"""
        evt = parse_sse_line(line)
        if evt is None:
            return None
        if evt.get("_done"):
            return evt
        self.feed(evt)
        return evt

    def result(self) -> dict:
        content = "".join(self.content_parts)
        message: Dict[str, Any] = {
            "role": self.role,
            "content": content if content else None,
        }
        if self.reasoning_parts:
            message["reasoning_content"] = "".join(self.reasoning_parts)
        if self.tool_calls:
            message["tool_calls"] = [self.tool_calls[i] for i in sorted(self.tool_calls.keys())]

        return {
            "id": self.response_id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "system_fingerprint": self.system_fingerprint,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "logprobs": None,
                    "finish_reason": self.finish_reason or "stop",
                }
            ],
            "usage": self.usage or {},
        }


# ========== Anthropic Messages Merger ==========


class AnthropicMessagesMerger:
    """
    将 Anthropic Messages API 的 SSE 事件序列合并成完整的 message 对象。

    产出等价于 stream=false 调用的响应：
        {
            "id": "msg_...",
            "type": "message",
            "role": "assistant",
            "model": "claude-...",
            "content": [
                {"type": "text", "text": "..."},
                {"type": "thinking", "thinking": "...", "signature": "..."},
                {"type": "tool_use", "id": "toolu_...", "name": "...", "input": {...}},
                {"type": "image", "source": {...}}
            ],
            "stop_reason": "end_turn|tool_use|max_tokens|...",
            "stop_sequence": null,
            "usage": {
                "input_tokens": ...,
                "output_tokens": ...,
                "cache_creation_input_tokens": ...,
                "cache_read_input_tokens": ...
            }
        }
    """

    def __init__(self):
        self.message: Optional[Dict[str, Any]] = None
        self.content_blocks: Dict[int, Dict[str, Any]] = {}
        self.tool_input_buffers: Dict[int, str] = {}
        self.stop_reason: Optional[str] = None
        self.stop_sequence: Optional[str] = None
        self.usage: Dict[str, Any] = {}

    def feed(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        etype = event.get("type")

        if etype == "message_start":
            self.message = json.loads(json.dumps(event.get("message", {})))
            msg_usage = self.message.get("usage", {}) or {}
            self.usage = dict(msg_usage)

        elif etype == "content_block_start":
            idx = event.get("index", 0)
            block = event.get("content_block", {}) or {}
            # 深拷贝，避免后续 feed 污染原始引用
            self.content_blocks[idx] = json.loads(json.dumps(block))
            if block.get("type") == "tool_use":
                self.tool_input_buffers[idx] = ""
                if "input" not in self.content_blocks[idx]:
                    self.content_blocks[idx]["input"] = {}

        elif etype == "content_block_delta":
            idx = event.get("index", 0)
            delta = event.get("delta", {}) or {}
            dtype = delta.get("type")

            if idx not in self.content_blocks:
                return
            block = self.content_blocks[idx]

            if dtype == "text_delta":
                block["text"] = block.get("text", "") + (delta.get("text", "") or "")

            elif dtype == "input_json_delta":
                # tool_use 的 input 通过累积 partial_json 字符串后整体 parse
                if idx not in self.tool_input_buffers:
                    self.tool_input_buffers[idx] = ""
                self.tool_input_buffers[idx] += delta.get("partial_json", "") or ""

            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + (delta.get("thinking", "") or "")

            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + (delta.get("signature", "") or "")

        elif etype == "content_block_stop":
            idx = event.get("index", 0)
            # tool_use 的 input 在 stop 时解析累积的 JSON 字符串
            if idx in self.tool_input_buffers:
                raw_json = self.tool_input_buffers[idx]
                try:
                    parsed = json.loads(raw_json) if raw_json else {}
                except json.JSONDecodeError:
                    parsed = {"_raw": raw_json}
                self.content_blocks[idx]["input"] = parsed

        elif etype == "message_delta":
            delta = event.get("delta", {}) or {}
            if "stop_reason" in delta:
                self.stop_reason = delta.get("stop_reason")
            if "stop_sequence" in delta:
                self.stop_sequence = delta.get("stop_sequence")
            usage_delta = event.get("usage", {}) or {}
            if usage_delta:
                for k, v in usage_delta.items():
                    self.usage[k] = v

        elif etype == "message_stop":
            pass

    def feed_raw_line(self, line: str) -> Optional[dict]:
        """喂入原始 SSE 行，返回解析出的事件（None 表示忽略）"""
        evt = parse_sse_line(line)
        if evt is None:
            return None
        if evt.get("_done"):
            return evt
        self.feed(evt)
        return evt

    def result(self) -> dict:
        blocks = [self.content_blocks[i] for i in sorted(self.content_blocks.keys())]
        if self.message is None:
            # 没收到 message_start（异常情况）
            return {
                "id": None,
                "type": "message",
                "role": "assistant",
                "model": None,
                "content": blocks,
                "stop_reason": self.stop_reason,
                "stop_sequence": self.stop_sequence,
                "usage": self.usage,
            }
        return {
            "id": self.message.get("id"),
            "type": "message",
            "role": self.message.get("role", "assistant"),
            "model": self.message.get("model"),
            "content": blocks,
            "stop_reason": self.stop_reason,
            "stop_sequence": self.stop_sequence,
            "usage": self.usage,
        }
