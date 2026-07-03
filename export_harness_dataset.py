"""
Export captured llm-tap calls into a canonical harness trajectory dataset.

The canonical format is intentionally provider-neutral. It preserves agent
messages, tool calls, tool outputs, and raw protocol fragments so downstream
exporters can later compile it to OpenAI, ShareGPT, ChatML, TRL, LLaMA-Factory,
or tool-SFT formats without rereading the raw capture files.
"""

import argparse
import copy
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_DATA_DIR = os.path.expanduser("~/.llm-tap")
DEFAULT_DB_PATH = os.path.join(DEFAULT_DATA_DIR, "calls.db")


def load_call_rows(db_path: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = """
        SELECT call_id, protocol, upstream_provider, upstream_model,
               started_at, finished_at, duration_ms, upstream_status,
               stop_reason, raw_path, is_stream
        FROM calls
        ORDER BY started_at ASC
    """
    if limit:
        sql += " LIMIT ?"
        params: Tuple[Any, ...] = (limit,)
    else:
        params = ()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def resolve_raw_path(db_path: str, raw_path: Optional[str]) -> Optional[str]:
    if not raw_path:
        return None
    if os.path.isabs(raw_path):
        return raw_path
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), raw_path)


def read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_jsonl(path: str, rows: Iterable[Dict[str, Any]]) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def dump_json_array(path: str, rows: Iterable[Dict[str, Any]]) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = list(rows)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return len(data)


def text_from_content_parts(parts: Any) -> str:
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return "" if parts is None else str(parts)

    texts: List[str] = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if text is None:
                text = part.get("output_text")
            if text is None:
                text = part.get("input_text")
            if text is not None:
                texts.append(str(text))
    return "\n".join(t for t in texts if t)


def compact_raw(obj: Dict[str, Any], drop_keys: Iterable[str] = ()) -> Dict[str, Any]:
    drops = set(drop_keys)
    return {k: v for k, v in obj.items() if k not in drops}


def normalize_tool_definition(tool: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(tool.get("function"), dict):
        fn = tool.get("function") or {}
        return {
            "type": tool.get("type") or "function",
            "name": fn.get("name"),
            "description": fn.get("description"),
            "parameters": fn.get("parameters"),
            "raw": tool,
        }
    return {
        "type": tool.get("type") or "function",
        "name": tool.get("name"),
        "description": tool.get("description"),
        "parameters": tool.get("parameters"),
        "raw": tool,
    }


def tool_definition_for_training(tool: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": tool.get("type") or "function",
        "function": {
            "name": tool.get("name"),
            "description": tool.get("description") or "",
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
        },
    }


def normalize_tool_definitions(request: Dict[str, Any]) -> List[Dict[str, Any]]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return []
    return [normalize_tool_definition(tool) for tool in tools if isinstance(tool, dict)]


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def normalize_chat_message(message: Dict[str, Any]) -> Dict[str, Any]:
    message_type = "tool_result" if message.get("role") == "tool" else "message"
    out: Dict[str, Any] = {
        "type": message_type,
        "role": message.get("role") or "assistant",
    }
    if "name" in message:
        out["name"] = message.get("name")
    if "content" in message:
        out["content"] = message.get("content")
    if "reasoning_content" in message:
        out["reasoning"] = message.get("reasoning_content")
    if message.get("tool_calls"):
        out["tool_calls"] = message.get("tool_calls")
    if message.get("tool_call_id"):
        out["tool_call_id"] = message.get("tool_call_id")
    out["raw"] = message
    return out


def normalize_responses_message(item: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    role = item.get("role") or "assistant"
    out: Dict[str, Any] = {
        "type": "message",
        "role": role,
        "content": text_from_content_parts(item.get("content")),
        "source": source,
        "raw": item,
    }
    if item.get("phase"):
        out["phase"] = item.get("phase")
    return out


def normalize_tool_call(item: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    item_type = item.get("type") or "function_call"
    name = item.get("name") or item_type
    arguments = item.get("arguments")
    if arguments is None and "input" in item:
        arguments = item.get("input")
    if arguments is None and "action" in item:
        arguments = item.get("action")

    return {
        "type": "tool_call",
        "role": "assistant",
        "source": source,
        "tool_calls": [
            {
                "id": item.get("call_id") or item.get("id"),
                "type": item_type,
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            }
        ],
        "raw": item,
    }


def normalize_tool_output(item: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    return {
        "type": "tool_result",
        "role": "tool",
        "source": source,
        "tool_call_id": item.get("call_id") or item.get("id"),
        "name": item.get("name"),
        "content": item.get("output"),
        "raw": item,
    }


def normalize_reasoning(item: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    return {
        "type": "reasoning",
        "role": "assistant",
        "source": source,
        "summary": item.get("summary"),
        "encrypted_content": item.get("encrypted_content"),
        "raw": item,
    }


def normalize_responses_item(item: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    item_type = item.get("type")
    if item_type == "message":
        return normalize_responses_message(item, source=source)
    if item_type in {"function_call", "custom_tool_call", "web_search_call"}:
        return normalize_tool_call(item, source=source)
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        return normalize_tool_output(item, source=source)
    if item_type == "reasoning":
        return normalize_reasoning(item, source=source)
    return {
        "type": item_type or "unknown",
        "role": item.get("role") or "assistant",
        "source": source,
        "raw": item,
    }


def extract_harness(messages: List[Dict[str, Any]], request: Dict[str, Any]) -> Dict[str, Any]:
    text = "\n".join(
        str(m.get("content") or "")
        for m in messages
        if m.get("role") in {"system", "developer", "user"}
    )
    harness: Dict[str, Any] = {
        "mode": None,
        "sandbox_mode": None,
        "approval_policy": None,
        "cwd": None,
        "tools": [],
    }

    for mode in ("Default", "Plan"):
        if f"Collaboration Mode: {mode}" in text:
            harness["mode"] = mode
            break

    markers = {
        "sandbox_mode": "`sandbox_mode` is `",
        "approval_policy": "Approval policy is currently ",
        "cwd": "<cwd>",
    }
    if markers["sandbox_mode"] in text:
        tail = text.split(markers["sandbox_mode"], 1)[1]
        harness["sandbox_mode"] = tail.split("`", 1)[0]
    if markers["approval_policy"] in text:
        tail = text.split(markers["approval_policy"], 1)[1]
        harness["approval_policy"] = tail.split(".", 1)[0].strip()
    if markers["cwd"] in text and "</cwd>" in text:
        harness["cwd"] = text.split(markers["cwd"], 1)[1].split("</cwd>", 1)[0].strip()

    tools = request.get("tools")
    if isinstance(tools, list):
        names = []
        for tool in tools:
            if isinstance(tool, dict):
                fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
                names.append(fn.get("name") or tool.get("name") or tool.get("type"))
        harness["tools"] = sorted({name for name in names if name})
    return harness


def convert_openai_chat(row: Dict[str, Any], raw: Dict[str, Any], raw_path: str) -> Dict[str, Any]:
    request = raw.get("request") or {}
    response = raw.get("response") or {}
    messages = [normalize_chat_message(m) for m in request.get("messages") or [] if isinstance(m, dict)]

    choices = response.get("choices") or []
    if choices and isinstance(choices[0], dict):
        assistant = choices[0].get("message")
        if isinstance(assistant, dict):
            messages.append(normalize_chat_message(assistant))

    return build_episode(row, raw, raw_path, messages, request, response)


def convert_openai_responses(row: Dict[str, Any], raw: Dict[str, Any], raw_path: str) -> Dict[str, Any]:
    request = raw.get("request") or {}
    response = raw.get("response") or {}
    messages: List[Dict[str, Any]] = []

    if request.get("instructions"):
        messages.append({
            "type": "message",
            "role": "developer",
            "content": request.get("instructions"),
            "source": "request.instructions",
        })

    for item in request.get("input") or []:
        if isinstance(item, dict):
            messages.append(normalize_responses_item(item, source="request.input"))

    for item in response.get("output") or []:
        if isinstance(item, dict):
            messages.append(normalize_responses_item(item, source="response.output"))

    return build_episode(row, raw, raw_path, messages, request, response)


def convert_anthropic_messages(row: Dict[str, Any], raw: Dict[str, Any], raw_path: str) -> Dict[str, Any]:
    request = raw.get("request") or {}
    response = raw.get("response") or {}
    messages: List[Dict[str, Any]] = []

    system = request.get("system")
    if system:
        messages.append({"type": "message", "role": "system", "content": text_from_content_parts(system)})

    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            continue
        messages.append({
            "type": "message",
            "role": message.get("role") or "user",
            "content": text_from_content_parts(message.get("content")),
            "raw": message,
        })

    if response.get("content"):
        messages.append({
            "type": "message",
            "role": "assistant",
            "content": text_from_content_parts(response.get("content")),
            "raw": response,
        })

    return build_episode(row, raw, raw_path, messages, request, response)


def build_episode(
    row: Dict[str, Any],
    raw: Dict[str, Any],
    raw_path: str,
    messages: List[Dict[str, Any]],
    request: Dict[str, Any],
    response: Dict[str, Any],
) -> Dict[str, Any]:
    protocol = row.get("protocol")
    call_id = row.get("call_id")
    tool_call_count = sum(len(m.get("tool_calls") or []) for m in messages)
    tool_result_count = sum(1 for m in messages if m.get("type") == "tool_result" or m.get("role") == "tool")
    assistant_messages = sum(1 for m in messages if m.get("role") == "assistant" and m.get("type") == "message")

    return {
        "schema": "llm-tap.harness_trajectory.v1",
        "id": f"episode-{call_id}",
        "source": {
            "call_ids": [call_id],
            "raw_path": raw_path,
            "protocol": protocol,
            "provider": row.get("upstream_provider"),
            "model": row.get("upstream_model"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "response_id": response.get("id") if isinstance(response, dict) else None,
            "previous_response_id": (
                request.get("previous_response_id") if isinstance(request, dict) else None
            ) or (response.get("previous_response_id") if isinstance(response, dict) else None),
        },
        "harness": extract_harness(messages, request if isinstance(request, dict) else {}),
        "tools": normalize_tool_definitions(request if isinstance(request, dict) else {}),
        "messages": messages,
        "labels": {
            "protocol": protocol,
            "success": row.get("upstream_status") == 200,
            "requires_tools": tool_call_count > 0,
            "has_tool_results": tool_result_count > 0,
            "has_assistant_message": assistant_messages > 0,
            "stop_reason": row.get("stop_reason"),
        },
        "quality": {
            "skip": not messages or assistant_messages == 0,
            "skip_reason": None if messages and assistant_messages > 0 else "no_assistant_message",
        },
        "stats": {
            "message_count": len(messages),
            "assistant_message_count": assistant_messages,
            "tool_call_count": tool_call_count,
            "tool_result_count": tool_result_count,
        },
        "exported_at": datetime.now().isoformat(timespec="seconds"),
    }


def convert_call(row: Dict[str, Any], db_path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    raw_path = resolve_raw_path(db_path, row.get("raw_path"))
    if not raw_path or not os.path.exists(raw_path):
        return None, "missing_raw_file"
    try:
        raw = read_json(raw_path)
    except Exception as exc:
        return None, f"read_error:{type(exc).__name__}"

    protocol = row.get("protocol")
    if protocol == "openai-chat":
        return convert_openai_chat(row, raw, raw_path), None
    if protocol == "openai-responses":
        return convert_openai_responses(row, raw, raw_path), None
    if protocol == "anthropic-messages":
        return convert_anthropic_messages(row, raw, raw_path), None
    return None, f"unsupported_protocol:{protocol}"


def iter_episodes(db_path: str, limit: Optional[int] = None):
    for row in load_call_rows(db_path, limit=limit):
        episode, error = convert_call(row, db_path)
        yield row, episode, error


def percentile(values: List[int], pct: float) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    idx = int(math.ceil((pct / 100.0) * len(ordered))) - 1
    idx = min(max(idx, 0), len(ordered) - 1)
    return ordered[idx]


def next_tool_results_for_target(messages: List[Dict[str, Any]], target_idx: int) -> List[Dict[str, Any]]:
    target = messages[target_idx]
    tool_calls = target.get("tool_calls") or []
    tool_call_ids = {tool_call.get("id") for tool_call in tool_calls if tool_call.get("id")}
    if not tool_call_ids:
        return []

    results: List[Dict[str, Any]] = []
    idx = target_idx + 1
    while idx < len(messages) and messages[idx].get("role") == "tool":
        tool_call_id = messages[idx].get("tool_call_id")
        if tool_call_id in tool_call_ids:
            results.append(messages[idx])
        idx += 1
    return results


def minimal_window_units_for_episode(
    episode: Dict[str, Any],
    *,
    chars_per_token: float = 4.0,
) -> Tuple[List[Dict[str, Any]], Counter]:
    skipped = Counter()
    item = openai_from_episode(episode, include_metadata=False)
    if item is None:
        skipped["empty_openai_messages"] += 1
        return [], skipped

    messages = item.get("messages") or []
    first_assistant_idx = next(
        (idx for idx, message in enumerate(messages) if message.get("role") == "assistant"),
        None,
    )
    if first_assistant_idx is None:
        skipped["no_assistant_target"] += 1
        return [], skipped

    prefix = messages[:first_assistant_idx]
    rows: List[Dict[str, Any]] = []
    for target_idx, target in enumerate(messages):
        if target.get("role") != "assistant":
            continue
        if not target.get("content") and not target.get("tool_calls"):
            skipped["empty_assistant_target"] += 1
            continue

        target_turn = [target] + next_tool_results_for_target(messages, target_idx)
        units = estimate_openai_messages_units(prefix + target_turn, chars_per_token)
        rows.append({
            "episode_id": episode.get("id"),
            "target_message_index": target_idx,
            "estimated_min_units": units,
            "prefix_units": estimate_openai_messages_units(prefix, chars_per_token),
            "target_turn_units": estimate_openai_messages_units(target_turn, chars_per_token),
            "target_has_tool_calls": bool(target.get("tool_calls")),
            "target_tool_result_count": max(0, len(target_turn) - 1),
        })
    if not rows:
        skipped["empty_minimal_windows"] += 1
    return rows, skipped


def inspect_window_budget(
    db_path: str,
    limit: Optional[int] = None,
    chars_per_token: float = 4.0,
    preview: int = 5,
) -> Dict[str, Any]:
    skipped = Counter()
    rows: List[Dict[str, Any]] = []
    calls = 0
    episodes = 0

    for _, episode, error in iter_episodes(db_path, limit=limit):
        calls += 1
        if error:
            skipped[error] += 1
            continue
        assert episode is not None
        episodes += 1
        if episode["quality"]["skip"]:
            skipped[episode["quality"]["skip_reason"]] += 1
            continue
        episode_rows, episode_skipped = minimal_window_units_for_episode(
            episode,
            chars_per_token=chars_per_token,
        )
        skipped.update(episode_skipped)
        rows.extend(episode_rows)

    units = [row["estimated_min_units"] for row in rows]
    sorted_rows = sorted(rows, key=lambda row: row["estimated_min_units"], reverse=True)
    return {
        "calls": calls,
        "episodes": episodes,
        "assistant_targets": len(rows),
        "token_budget_mode": "json_char_count_divisor",
        "chars_per_token": chars_per_token,
        "description": "Minimum estimated --max-seq-len to keep fixed prefix plus at least the target assistant turn. If the target calls tools, its immediate tool results are included as one complete turn.",
        "recommended_min_max_seq_len": max(units) if units else None,
        "p50_min_max_seq_len": percentile(units, 50),
        "p90_min_max_seq_len": percentile(units, 90),
        "p95_min_max_seq_len": percentile(units, 95),
        "p99_min_max_seq_len": percentile(units, 99),
        "skipped": dict(skipped),
        "largest_targets": sorted_rows[:preview],
    }


def inspect_dataset(
    db_path: str,
    limit: Optional[int] = None,
    preview: int = 3,
    include_window_budget: bool = False,
    chars_per_token: float = 4.0,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "db_path": db_path,
        "calls": 0,
        "episodes": 0,
        "skipped": 0,
        "protocols": Counter(),
        "providers": Counter(),
        "models": Counter(),
        "skip_reasons": Counter(),
        "message_types": Counter(),
        "roles": Counter(),
        "tool_names": Counter(),
        "harness_modes": Counter(),
        "previews": [],
    }

    for row, episode, error in iter_episodes(db_path, limit=limit):
        report["calls"] += 1
        report["protocols"][row.get("protocol")] += 1
        report["providers"][row.get("upstream_provider")] += 1
        report["models"][row.get("upstream_model")] += 1
        if error:
            report["skipped"] += 1
            report["skip_reasons"][error] += 1
            continue
        assert episode is not None
        report["episodes"] += 1
        if episode["quality"]["skip"]:
            report["skip_reasons"][episode["quality"]["skip_reason"]] += 1
        report["harness_modes"][episode["harness"].get("mode") or "unknown"] += 1
        for message in episode["messages"]:
            report["message_types"][message.get("type")] += 1
            report["roles"][message.get("role")] += 1
            for tool_call in message.get("tool_calls") or []:
                fn = tool_call.get("function") or {}
                report["tool_names"][fn.get("name") or tool_call.get("type") or "unknown"] += 1
        if len(report["previews"]) < preview:
            report["previews"].append({
                "id": episode["id"],
                "protocol": episode["source"]["protocol"],
                "model": episode["source"]["model"],
                "harness": episode["harness"],
                "stats": episode["stats"],
                "first_messages": [
                    {
                        "role": m.get("role"),
                        "type": m.get("type"),
                        "content": str(m.get("content") or m.get("summary") or "")[:240],
                    }
                    for m in episode["messages"][:5]
                ],
            })

    for key in ("protocols", "providers", "models", "skip_reasons", "message_types", "roles", "tool_names", "harness_modes"):
        report[key] = dict(report[key])
    if include_window_budget:
        report["window_budget"] = inspect_window_budget(
            db_path,
            limit=limit,
            chars_per_token=chars_per_token,
            preview=preview,
        )
    return report


def export_dataset(db_path: str, out_path: str, limit: Optional[int] = None, include_skipped: bool = False) -> Dict[str, Any]:
    exported = 0
    skipped = Counter()

    def rows():
        nonlocal exported
        for row, episode, error in iter_episodes(db_path, limit=limit):
            if error:
                skipped[error] += 1
                continue
            assert episode is not None
            if episode["quality"]["skip"] and not include_skipped:
                skipped[episode["quality"]["skip_reason"]] += 1
                continue
            exported += 1
            yield episode

    written = dump_jsonl(out_path, rows())
    return {
        "db_path": db_path,
        "out_path": out_path,
        "format": "canonical",
        "exported": exported,
        "written": written,
        "skipped": dict(skipped),
    }


def sharegpt_from_episode(
    episode: Dict[str, Any],
    include_metadata: bool = False,
    include_tools: bool = True,
) -> Optional[Dict[str, Any]]:
    conversations: List[Dict[str, str]] = []
    role_map = {
        "system": "system",
        "developer": "system",
        "user": "human",
        "assistant": "gpt",
        "tool": "observation",
    }

    tools = episode.get("tools") or []
    if include_tools and tools:
        tool_defs = [
            compact_raw(tool, drop_keys=("raw",))
            for tool in tools
        ]
        conversations.append({
            "from": "system",
            "value": "<tools>\n" + json.dumps(tool_defs, ensure_ascii=False, indent=2) + "\n</tools>",
        })

    for message in episode.get("messages") or []:
        message_type = message.get("type")
        role = message.get("role")
        from_role = role_map.get(role, role or "unknown")
        value = ""

        if message_type == "tool_call":
            blocks = []
            for tool_call in message.get("tool_calls") or []:
                fn = tool_call.get("function") or {}
                name = fn.get("name") or tool_call.get("type") or "tool"
                args = as_text(fn.get("arguments"))
                blocks.append(f"<tool_call name=\"{name}\">\n{args}\n</tool_call>")
            value = "\n\n".join(blocks)
            from_role = "gpt"
        elif message_type == "tool_result":
            name = message.get("name") or "tool"
            content = as_text(message.get("content"))
            value = f"<tool_result name=\"{name}\">\n{content}\n</tool_result>"
            from_role = "observation"
        elif message_type == "reasoning":
            summary = as_text(message.get("summary"))
            if summary:
                value = f"<reasoning>\n{summary}\n</reasoning>"
                from_role = "gpt"
        else:
            content = as_text(message.get("content"))
            reasoning = as_text(message.get("reasoning"))
            if reasoning and role == "assistant":
                value = f"<reasoning>\n{reasoning}\n</reasoning>"
                if content:
                    value += "\n\n" + content
            else:
                value = content

            if message.get("tool_calls"):
                blocks = []
                for tool_call in message.get("tool_calls") or []:
                    fn = tool_call.get("function") or {}
                    name = fn.get("name") or tool_call.get("type") or "tool"
                    args = as_text(fn.get("arguments"))
                    blocks.append(f"<tool_call name=\"{name}\">\n{args}\n</tool_call>")
                value = "\n\n".join([v for v in [value, "\n\n".join(blocks)] if v])

        if value:
            conversations.append({"from": from_role, "value": value})

    if not conversations:
        return None

    item = {
        "id": episode.get("id"),
        "conversations": conversations,
    }
    if include_metadata:
        item["metadata"] = {
            "schema": "llm-tap.sharegpt.v1",
            "source": episode.get("source"),
            "harness": episode.get("harness"),
            "tools": episode.get("tools"),
            "labels": episode.get("labels"),
            "stats": episode.get("stats"),
        }
    return item


def export_sharegpt_dataset(
    db_path: str,
    out_path: str,
    limit: Optional[int] = None,
    include_skipped: bool = False,
    include_metadata: bool = False,
    include_tools: bool = True,
) -> Dict[str, Any]:
    exported = 0
    skipped = Counter()

    def rows():
        nonlocal exported
        for row, episode, error in iter_episodes(db_path, limit=limit):
            if error:
                skipped[error] += 1
                continue
            assert episode is not None
            if episode["quality"]["skip"] and not include_skipped:
                skipped[episode["quality"]["skip_reason"]] += 1
                continue
            item = sharegpt_from_episode(
                episode,
                include_metadata=include_metadata,
                include_tools=include_tools,
            )
            if item is None:
                skipped["empty_sharegpt_conversation"] += 1
                continue
            exported += 1
            yield item

    written = dump_json_array(out_path, rows())
    return {
        "db_path": db_path,
        "out_path": out_path,
        "format": "sharegpt",
        "include_metadata": include_metadata,
        "include_tools": include_tools,
        "exported": exported,
        "written": written,
        "skipped": dict(skipped),
    }


def tool_sft_message_from_episode_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    message_type = message.get("type")
    role = message.get("role")

    if message_type == "reasoning":
        summary = message.get("summary")
        if not summary:
            return None
        return {
            "role": "assistant",
            "reasoning_content": as_text(summary),
        }

    if message_type == "tool_call":
        tool_calls = []
        for tool_call in message.get("tool_calls") or []:
            fn = tool_call.get("function") or {}
            arguments = fn.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
            tool_calls.append({
                "id": tool_call.get("id"),
                "type": "function",
                "function": {
                    "name": fn.get("name"),
                    "arguments": arguments,
                },
            })
        if not tool_calls:
            return None
        return {"role": "assistant", "tool_calls": tool_calls}

    if message_type == "tool_result" or role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.get("tool_call_id"),
            "content": as_text(message.get("content")),
        }

    if role not in {"system", "developer", "user", "assistant", "tool"}:
        return None

    out: Dict[str, Any] = {"role": role}
    reasoning = as_text(message.get("reasoning"))
    if role == "assistant" and reasoning:
        out["reasoning_content"] = reasoning
    content = as_text(message.get("content"))
    if content:
        out["content"] = content

    tool_calls = []
    for tool_call in message.get("tool_calls") or []:
        fn = tool_call.get("function") or {}
        arguments = fn.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
        tool_calls.append({
            "id": tool_call.get("id"),
            "type": "function",
            "function": {
                "name": fn.get("name"),
                "arguments": arguments,
            },
        })
    if tool_calls:
        out["tool_calls"] = tool_calls

    if role == "assistant" and not out.get("content") and not out.get("tool_calls") and not out.get("reasoning_content"):
        return None
    if role in {"system", "developer", "user"} and not out.get("content"):
        return None
    return out


def tool_sft_from_episode(episode: Dict[str, Any], include_metadata: bool = False) -> Optional[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    for message in episode.get("messages") or []:
        converted = tool_sft_message_from_episode_message(message)
        if converted:
            messages.append(converted)

    if not messages:
        return None

    item: Dict[str, Any] = {
        "tools": [tool_definition_for_training(tool) for tool in episode.get("tools") or []],
        "messages": messages,
    }
    if include_metadata:
        item["metadata"] = {
            "id": episode.get("id"),
            "schema": "llm-tap.tool_sft.v1",
            "source": episode.get("source"),
            "harness": episode.get("harness"),
            "labels": episode.get("labels"),
            "stats": episode.get("stats"),
        }
    return item


def export_tool_sft_dataset(
    db_path: str,
    out_path: str,
    limit: Optional[int] = None,
    include_skipped: bool = False,
    include_metadata: bool = False,
) -> Dict[str, Any]:
    exported = 0
    skipped = Counter()

    def rows():
        nonlocal exported
        for row, episode, error in iter_episodes(db_path, limit=limit):
            if error:
                skipped[error] += 1
                continue
            assert episode is not None
            if episode["quality"]["skip"] and not include_skipped:
                skipped[episode["quality"]["skip_reason"]] += 1
                continue
            item = tool_sft_from_episode(episode, include_metadata=include_metadata)
            if item is None:
                skipped["empty_tool_sft_messages"] += 1
                continue
            exported += 1
            yield item

    written = dump_jsonl(out_path, rows())
    return {
        "db_path": db_path,
        "out_path": out_path,
        "format": "tool_sft",
        "include_metadata": include_metadata,
        "exported": exported,
        "written": written,
        "skipped": dict(skipped),
    }


def reasoning_text(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts: List[str] = []
        for item in value:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("summary_text") or item.get("content")
                if text:
                    texts.append(str(text))
        return "\n".join(texts)
    return as_text(value)


def with_think_block(content: str, thoughts: List[str]) -> str:
    cleaned = [text.strip() for text in thoughts if text and text.strip()]
    if not cleaned:
        return content
    think = "<think>\n" + "\n\n".join(cleaned) + "\n</think>"
    return "\n".join(part for part in (think, content) if part)


def tool_arguments_string(arguments: Any) -> str:
    if arguments is None:
        return "{}"
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def openai_tool_call(tool_call: Dict[str, Any], fallback_id: str) -> Dict[str, Any]:
    fn = tool_call.get("function") or {}
    return {
        "id": tool_call.get("id") or fallback_id,
        "type": "function",
        "function": {
            "name": fn.get("name") or tool_call.get("name") or tool_call.get("type") or "tool",
            "arguments": tool_arguments_string(fn.get("arguments")),
        },
    }


def openai_from_episode(
    episode: Dict[str, Any],
    include_metadata: bool = False,
) -> Optional[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    pending_assistant: Optional[Dict[str, Any]] = None
    pending_reasoning: List[str] = []
    tool_names_by_id: Dict[str, str] = {}
    tool_call_seq = 0

    def flush_assistant() -> None:
        nonlocal pending_assistant, pending_reasoning
        if not pending_assistant:
            return
        content = as_text(pending_assistant.get("content"))
        content = with_think_block(content, pending_reasoning)
        pending_reasoning = []

        out: Dict[str, Any] = {"role": "assistant", "content": content}
        tool_calls = pending_assistant.get("tool_calls") or []
        if tool_calls:
            out["tool_calls"] = tool_calls
        if out["content"] or out.get("tool_calls"):
            messages.append(out)
        pending_assistant = None

    for message in episode.get("messages") or []:
        message_type = message.get("type")
        role = message.get("role")

        if message_type == "reasoning":
            text = reasoning_text(message.get("summary"))
            if text:
                pending_reasoning.append(text)
            continue

        if message_type == "tool_call":
            if pending_assistant is None:
                pending_assistant = {"role": "assistant", "content": "", "tool_calls": []}
            for tool_call in message.get("tool_calls") or []:
                tool_call_seq += 1
                fallback_id = f"call_{episode.get('id', 'episode').replace('-', '_')}_{tool_call_seq}"
                converted = openai_tool_call(tool_call, fallback_id)
                pending_assistant.setdefault("tool_calls", []).append(converted)
                tool_names_by_id[converted["id"]] = converted["function"]["name"]
            continue

        if message_type == "tool_result" or role == "tool":
            flush_assistant()
            tool_call_id = message.get("tool_call_id")
            if not tool_call_id:
                continue
            out = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": as_text(message.get("content")),
            }
            name = message.get("name") or tool_names_by_id.get(tool_call_id)
            if name:
                out["name"] = name
            messages.append(out)
            continue

        if role == "assistant":
            if pending_assistant is None:
                pending_assistant = {"role": "assistant", "content": "", "tool_calls": []}
            content = as_text(message.get("content"))
            reasoning = reasoning_text(message.get("reasoning"))
            if reasoning:
                pending_reasoning.append(reasoning)
            if content:
                existing = as_text(pending_assistant.get("content"))
                pending_assistant["content"] = "\n\n".join(part for part in (existing, content) if part)
            for tool_call in message.get("tool_calls") or []:
                tool_call_seq += 1
                fallback_id = f"call_{episode.get('id', 'episode').replace('-', '_')}_{tool_call_seq}"
                converted = openai_tool_call(tool_call, fallback_id)
                pending_assistant.setdefault("tool_calls", []).append(converted)
                tool_names_by_id[converted["id"]] = converted["function"]["name"]
            continue

        if role in {"system", "developer", "user"}:
            flush_assistant()
            content = as_text(message.get("content"))
            if not content:
                continue
            messages.append({
                "role": "system" if role == "developer" else role,
                "content": content,
            })

    flush_assistant()

    if not messages:
        return None

    item: Dict[str, Any] = {"messages": messages}
    if include_metadata:
        item["metadata"] = {
            "id": episode.get("id"),
            "schema": "llm-tap.openai_chat_finetune.v1",
            "source": episode.get("source"),
            "harness": episode.get("harness"),
            "labels": episode.get("labels"),
            "stats": episode.get("stats"),
        }
    return item


def export_openai_dataset(
    db_path: str,
    out_path: str,
    limit: Optional[int] = None,
    include_skipped: bool = False,
    include_metadata: bool = False,
) -> Dict[str, Any]:
    exported = 0
    skipped = Counter()

    def rows():
        nonlocal exported
        for row, episode, error in iter_episodes(db_path, limit=limit):
            if error:
                skipped[error] += 1
                continue
            assert episode is not None
            if episode["quality"]["skip"] and not include_skipped:
                skipped[episode["quality"]["skip_reason"]] += 1
                continue
            item = openai_from_episode(episode, include_metadata=include_metadata)
            if item is None:
                skipped["empty_openai_messages"] += 1
                continue
            exported += 1
            yield item

    written = dump_jsonl(out_path, rows())
    return {
        "db_path": db_path,
        "out_path": out_path,
        "format": "openai",
        "include_metadata": include_metadata,
        "exported": exported,
        "written": written,
        "skipped": dict(skipped),
    }


def estimate_openai_message_units(message: Dict[str, Any], chars_per_token: float = 4.0) -> int:
    chars = len(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
    return max(1, int(math.ceil(chars / max(chars_per_token, 0.1))))


def estimate_openai_messages_units(messages: List[Dict[str, Any]], chars_per_token: float = 4.0) -> int:
    if not messages:
        return 1
    return sum(estimate_openai_message_units(message, chars_per_token) for message in messages) + len(messages) + 1


def flatten_message_groups(groups: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    for group in groups:
        messages.extend(group)
    return messages


def openai_history_groups(
    messages: List[Dict[str, Any]],
    start: int,
    end: int,
) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    idx = start
    while idx < end:
        message = messages[idx]
        if message.get("role") != "assistant":
            groups.append([message])
            idx += 1
            continue

        group = [message]
        idx += 1
        tool_calls = message.get("tool_calls") or []
        tool_call_ids = {tool_call.get("id") for tool_call in tool_calls if tool_call.get("id")}
        while idx < end and messages[idx].get("role") == "tool":
            tool_call_id = messages[idx].get("tool_call_id")
            if tool_call_ids and tool_call_id and tool_call_id not in tool_call_ids:
                break
            group.append(messages[idx])
            idx += 1
        groups.append(group)
    return groups


def compact_group_to_budget(
    group: List[Dict[str, Any]],
    max_units: int,
    chars_per_token: float = 4.0,
) -> Optional[List[Dict[str, Any]]]:
    if estimate_openai_messages_units(group, chars_per_token) <= max_units:
        return copy.deepcopy(group)

    compacted = copy.deepcopy(group)
    tool_indexes = [
        idx for idx, message in enumerate(compacted)
        if message.get("role") == "tool" and isinstance(message.get("content"), str)
    ]
    if not tool_indexes:
        return None

    original_content = compacted[tool_indexes[-1]].get("content") or ""
    for idx in tool_indexes:
        compacted[idx]["content"] = ""
    if estimate_openai_messages_units(compacted, chars_per_token) > max_units:
        return None

    marker = "[...truncated...]\n"
    best = copy.deepcopy(compacted)
    low = 0
    high = len(original_content)
    while low <= high:
        mid = (low + high) // 2
        candidate = copy.deepcopy(compacted)
        if mid >= len(original_content):
            content = original_content
        else:
            suffix = original_content[-mid:] if mid else ""
            content = marker + suffix if suffix else ""
        candidate[tool_indexes[-1]]["content"] = content
        if estimate_openai_messages_units(candidate, chars_per_token) <= max_units:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def compact_prefix_to_budget(
    prefix: List[Dict[str, Any]],
    target: List[Dict[str, Any]],
    max_units: int,
    chars_per_token: float = 4.0,
) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
    if estimate_openai_messages_units(prefix + target, chars_per_token) <= max_units:
        return copy.deepcopy(prefix), False

    compacted = copy.deepcopy(prefix)
    system_indexes = [
        idx for idx, message in enumerate(compacted)
        if message.get("role") == "system" and isinstance(message.get("content"), str)
    ]
    if not system_indexes:
        return None, False

    original_content = {idx: compacted[idx].get("content") or "" for idx in system_indexes}
    for idx in system_indexes:
        compacted[idx]["content"] = ""
    if estimate_openai_messages_units(compacted + target, chars_per_token) > max_units:
        return None, True

    marker = "\n[...system truncated for window budget...]"
    for idx in system_indexes:
        source = original_content[idx]
        low = 0
        high = len(source)
        best_content = ""
        while low <= high:
            mid = (low + high) // 2
            if mid >= len(source):
                content = source
            else:
                content = source[:mid] + marker if mid else ""
            candidate = copy.deepcopy(compacted)
            candidate[idx]["content"] = content
            if estimate_openai_messages_units(candidate + target, chars_per_token) <= max_units:
                best_content = content
                low = mid + 1
            else:
                high = mid - 1
        compacted[idx]["content"] = best_content

    return compacted, True


def openai_windowed_from_episode(
    episode: Dict[str, Any],
    *,
    max_seq_len: int = 4096,
    chars_per_token: float = 4.0,
    prefix_budget_ratio: float = 0.45,
    include_metadata: bool = False,
) -> Tuple[List[Dict[str, Any]], Counter]:
    skipped = Counter()
    item = openai_from_episode(episode, include_metadata=False)
    if item is None:
        skipped["empty_openai_messages"] += 1
        return [], skipped

    messages = item.get("messages") or []
    first_assistant_idx = next(
        (idx for idx, message in enumerate(messages) if message.get("role") == "assistant"),
        None,
    )
    if first_assistant_idx is None:
        skipped["no_assistant_target"] += 1
        return [], skipped

    prefix = copy.deepcopy(messages[:first_assistant_idx])
    windows: List[Dict[str, Any]] = []

    for target_idx, target_message in enumerate(messages):
        if target_message.get("role") != "assistant":
            continue
        if not target_message.get("content") and not target_message.get("tool_calls"):
            skipped["empty_assistant_target"] += 1
            continue

        target = [copy.deepcopy(target_message)]
        target_units = estimate_openai_messages_units(target, chars_per_token)
        if target_units >= max_seq_len:
            skipped["target_exceeds_max_seq_len"] += 1
            continue

        prefix_budget = min(
            max_seq_len - target_units,
            max(1, int(max_seq_len * max(0.0, min(prefix_budget_ratio, 1.0)))),
        )
        compacted_prefix, prefix_compacted = compact_prefix_to_budget(
            prefix,
            [],
            prefix_budget,
            chars_per_token,
        )
        if compacted_prefix is None:
            skipped["prefix_target_exceeds_max_seq_len"] += 1
            continue
        if estimate_openai_messages_units(compacted_prefix + target, chars_per_token) > max_seq_len:
            skipped["prefix_target_exceeds_max_seq_len"] += 1
            continue

        selected_reversed: List[List[Dict[str, Any]]] = []
        groups = openai_history_groups(messages, first_assistant_idx, target_idx)
        for group in reversed(groups):
            selected = flatten_message_groups(list(reversed(selected_reversed)))
            candidate_messages = compacted_prefix + flatten_message_groups([copy.deepcopy(group)]) + selected + target
            if estimate_openai_messages_units(candidate_messages, chars_per_token) <= max_seq_len:
                selected_reversed.append(copy.deepcopy(group))
                continue

            current_messages = compacted_prefix + selected + target
            remaining = max_seq_len - estimate_openai_messages_units(current_messages, chars_per_token)
            compacted = compact_group_to_budget(group, remaining, chars_per_token)
            if compacted:
                candidate_messages = compacted_prefix + compacted + selected + target
                if estimate_openai_messages_units(candidate_messages, chars_per_token) <= max_seq_len:
                    selected_reversed.append(compacted)
            break

        history = flatten_message_groups(list(reversed(selected_reversed)))
        window_messages = compacted_prefix + history + target
        window: Dict[str, Any] = {"messages": window_messages}
        if include_metadata:
            window["metadata"] = {
                "id": episode.get("id"),
                "schema": "llm-tap.openai_chat_windowed.v1",
                "source": episode.get("source"),
                "window": {
                    "target_message_index": target_idx,
                    "max_seq_len": max_seq_len,
                    "estimated_units": estimate_openai_messages_units(window_messages, chars_per_token),
                    "estimate_mode": "json_char_count_divisor",
                    "chars_per_token": chars_per_token,
                    "prefix_budget_ratio": prefix_budget_ratio,
                    "prefix_budget_units": prefix_budget,
                    "history_message_count": len(history),
                    "prefix_message_count": len(compacted_prefix),
                    "prefix_compacted": prefix_compacted,
                },
            }
        windows.append(window)

    if not windows:
        skipped["empty_openai_windows"] += 1
    return windows, skipped


def export_openai_windowed_dataset(
    db_path: str,
    out_path: str,
    limit: Optional[int] = None,
    include_skipped: bool = False,
    include_metadata: bool = False,
    max_seq_len: int = 4096,
    chars_per_token: float = 4.0,
    prefix_budget_ratio: float = 0.45,
) -> Dict[str, Any]:
    exported = 0
    source_episodes = 0
    skipped = Counter()
    max_estimated_units = 0

    def rows():
        nonlocal exported, source_episodes, max_estimated_units
        for row, episode, error in iter_episodes(db_path, limit=limit):
            if error:
                skipped[error] += 1
                continue
            assert episode is not None
            if episode["quality"]["skip"] and not include_skipped:
                skipped[episode["quality"]["skip_reason"]] += 1
                continue
            windows, window_skipped = openai_windowed_from_episode(
                episode,
                max_seq_len=max_seq_len,
                chars_per_token=chars_per_token,
                prefix_budget_ratio=prefix_budget_ratio,
                include_metadata=include_metadata,
            )
            skipped.update(window_skipped)
            if not windows:
                continue
            source_episodes += 1
            for window in windows:
                exported += 1
                max_estimated_units = max(
                    max_estimated_units,
                    estimate_openai_messages_units(window.get("messages") or [], chars_per_token),
                )
                yield window

    written = dump_jsonl(out_path, rows())
    return {
        "db_path": db_path,
        "out_path": out_path,
        "format": "openai_windowed",
        "include_metadata": include_metadata,
        "max_seq_len": max_seq_len,
        "token_budget_mode": "json_char_count_divisor",
        "chars_per_token": chars_per_token,
        "prefix_budget_ratio": prefix_budget_ratio,
        "source_episodes": source_episodes,
        "exported": exported,
        "written": written,
        "max_estimated_units": max_estimated_units,
        "skipped": dict(skipped),
    }


def print_json(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export llm-tap calls as canonical harness trajectories.")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to calls.db")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_p = sub.add_parser("inspect", help="Inspect exportability and print a JSON report")
    inspect_p.add_argument("--preview", type=int, default=3, help="Number of episode previews")
    inspect_p.add_argument("--limit", type=int, default=None, help="Limit number of calls read")
    inspect_p.add_argument("--window-budget", action="store_true", help="Estimate minimum max_seq_len for openai_windowed exports")
    inspect_p.add_argument("--chars-per-token", type=float, default=4.0, help="Heuristic character/token divisor for window budget estimates")

    export_p = sub.add_parser("export", help="Export dataset")
    export_p.add_argument("--out", required=True, help="Output JSONL path")
    export_p.add_argument("--format", choices=["canonical", "sharegpt", "tool_sft", "openai", "openai_windowed"], default="canonical")
    export_p.add_argument("--include-skipped", action="store_true", help="Include low-quality/skipped episodes")
    export_p.add_argument("--include-metadata", action="store_true", help="Include metadata in supported outputs")
    export_p.add_argument("--no-tools", action="store_true", help="Do not inject tool definitions into ShareGPT system turns")
    export_p.add_argument("--max-seq-len", type=int, default=4096, help="Estimated max sequence length for windowed exports")
    export_p.add_argument("--chars-per-token", type=float, default=4.0, help="Heuristic character/token divisor for windowed exports")
    export_p.add_argument("--prefix-budget-ratio", type=float, default=0.45, help="Maximum window budget fraction reserved for fixed prefix in windowed exports")
    export_p.add_argument("--limit", type=int, default=None, help="Limit number of calls read")

    args = parser.parse_args()
    db_path = os.path.abspath(os.path.expanduser(args.db))

    if args.command == "inspect":
        print_json(inspect_dataset(
            db_path,
            limit=args.limit,
            preview=args.preview,
            include_window_budget=args.window_budget,
            chars_per_token=args.chars_per_token,
        ))
    elif args.command == "export":
        if args.format == "sharegpt":
            result = export_sharegpt_dataset(
                db_path,
                args.out,
                limit=args.limit,
                include_skipped=args.include_skipped,
                include_metadata=args.include_metadata,
                include_tools=not args.no_tools,
            )
        elif args.format == "tool_sft":
            result = export_tool_sft_dataset(
                db_path,
                args.out,
                limit=args.limit,
                include_skipped=args.include_skipped,
                include_metadata=args.include_metadata,
            )
        elif args.format == "openai":
            result = export_openai_dataset(
                db_path,
                args.out,
                limit=args.limit,
                include_skipped=args.include_skipped,
                include_metadata=args.include_metadata,
            )
        elif args.format == "openai_windowed":
            result = export_openai_windowed_dataset(
                db_path,
                args.out,
                limit=args.limit,
                include_skipped=args.include_skipped,
                include_metadata=args.include_metadata,
                max_seq_len=args.max_seq_len,
                chars_per_token=args.chars_per_token,
                prefix_budget_ratio=args.prefix_budget_ratio,
            )
        else:
            result = export_dataset(db_path, args.out, limit=args.limit, include_skipped=args.include_skipped)
        print_json(result)


if __name__ == "__main__":
    main()
