import unittest

from export_harness_dataset import (
    constrained_format_from_episode,
    convert_anthropic_messages,
    convert_openai_responses,
    estimate_context_item_units,
    estimate_openai_messages_units,
    openai_from_episode,
    openai_windowed_from_episode,
)


class OpenAIExportTests(unittest.TestCase):
    def _source_row(self, protocol):
        return {
            "call_id": "call-test",
            "protocol": protocol,
            "upstream_provider": "provider",
            "upstream_model": "model",
            "started_at": "2026-01-01T00:00:00",
            "finished_at": "2026-01-01T00:00:01",
            "upstream_status": 200,
            "stop_reason": "end_turn",
        }

    def test_responses_string_input_is_preserved(self):
        episode = convert_openai_responses(
            self._source_row("openai-responses"),
            {
                "request": {"input": "Explain this code"},
                "response": {"output": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Sure"}],
                }]},
            },
            "raw.json",
        )
        self.assertEqual(episode["messages"][0]["role"], "user")
        self.assertEqual(episode["messages"][0]["content"], "Explain this code")

    def test_anthropic_content_blocks_preserve_thinking_and_tools(self):
        episode = convert_anthropic_messages(
            self._source_row("anthropic-messages"),
            {
                "request": {"messages": [{
                    "role": "user",
                    "content": [{"type": "text", "text": "Look this up"}],
                }, {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "old-tool", "content": "old result"}],
                }]},
                "response": {"content": [
                    {"type": "thinking", "thinking": "Need a lookup", "signature": "sig"},
                    {"type": "redacted_thinking", "data": "opaque"},
                    {"type": "tool_use", "id": "tool-1", "name": "lookup", "input": {"q": "x"}},
                ]},
            },
            "raw.json",
        )
        self.assertEqual(
            [message["type"] for message in episode["messages"]],
            ["message", "tool_result", "reasoning", "reasoning", "tool_call"],
        )
        self.assertEqual(episode["messages"][2]["signature"], "sig")
        self.assertTrue(episode["messages"][3]["redacted"])
        self.assertEqual(episode["messages"][3]["summary"], "opaque")
        self.assertEqual(
            episode["messages"][4]["tool_calls"][0]["function"]["arguments"],
            {"q": "x"},
        )
        openai_item = openai_from_episode(episode)
        self.assertIsNotNone(openai_item)
        self.assertEqual(
            openai_item["messages"][-1]["tool_calls"][0]["function"]["name"],
            "lookup",
        )
        self.assertEqual(openai_item["messages"][1]["tool_call_id"], "old-tool")
        self.assertFalse(episode["quality"]["skip"])

    def _tool_episode(self):
        return {
            "id": "episode-tools",
            "source": {"protocol": "openai-responses", "model": "m"},
            "harness": {"mode": "Default"},
            "labels": {"requires_tools": True},
            "stats": {},
            "tools": [{
                "type": "function",
                "name": "lookup",
                "description": "Look up a value",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            }],
            "messages": [
                {"type": "message", "role": "system", "content": "Use tools carefully."},
                {"type": "message", "role": "user", "content": "First"},
                {"type": "tool_call", "role": "assistant", "tool_calls": [{
                    "id": "call-1", "function": {"name": "lookup", "arguments": {"q": "first"}},
                }]},
                {"type": "tool_result", "role": "tool", "tool_call_id": "call-1", "content": "x" * 900},
                {"type": "message", "role": "assistant", "content": "First answer"},
                {"type": "message", "role": "user", "content": "Second"},
                {"type": "message", "role": "assistant", "content": "Second answer"},
            ],
        }

    def test_preserves_reasoning_and_structured_tool_trajectory(self):
        episode = {
            "id": "episode-1",
            "messages": [
                {"type": "message", "role": "developer", "content": "Use tools."},
                {"type": "message", "role": "user", "content": "Look it up."},
                {"type": "reasoning", "summary": "Need a lookup."},
                {"type": "tool_call", "tool_calls": [{
                    "id": "call-1",
                    "function": {"name": "lookup", "arguments": {"q": "x"}},
                }]},
                {"type": "tool_result", "role": "tool", "tool_call_id": "call-1", "content": "result"},
                {"type": "message", "role": "assistant", "content": "Finished."},
            ],
        }

        item = openai_from_episode(episode)
        self.assertIsNotNone(item)
        messages = item["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": "Use tools."})
        self.assertIn("<think>\nNeed a lookup.\n</think>", messages[2]["content"])
        self.assertEqual(messages[2]["tool_calls"][0]["function"]["arguments"], '{"q":"x"}')
        self.assertEqual(messages[3]["tool_call_id"], "call-1")
        self.assertEqual(messages[-1], {"role": "assistant", "content": "Finished."})

    def test_windowed_samples_end_at_assistant_targets_within_budget(self):
        episode = {
            "id": "episode-2",
            "messages": [
                {"type": "message", "role": "system", "content": "Be concise."},
                {"type": "message", "role": "user", "content": "First"},
                {"type": "message", "role": "assistant", "content": "One"},
                {"type": "message", "role": "user", "content": "Second"},
                {"type": "message", "role": "assistant", "content": "Two"},
            ],
        }

        windows, skipped = openai_windowed_from_episode(
            episode,
            max_seq_len=64,
            chars_per_token=4.0,
        )
        self.assertEqual(len(windows), 2)
        self.assertFalse(skipped)
        for window in windows:
            messages = window["messages"]
            self.assertEqual(messages[-1]["role"], "assistant")
            self.assertLessEqual(estimate_openai_messages_units(messages, 4.0), 64)

    def test_context_limit_supports_all_primary_formats(self):
        episode = self._tool_episode()
        for export_format in ("canonical", "tool_sft", "openai", "sharegpt"):
            with self.subTest(export_format=export_format):
                items, skipped = constrained_format_from_episode(
                    episode,
                    export_format,
                    max_seq_len=180,
                    chars_per_token=4.0,
                    include_metadata=True,
                )
                self.assertTrue(items, skipped)
                for item in items:
                    self.assertLessEqual(
                        estimate_context_item_units(item, export_format, 4.0),
                        180,
                    )

    def test_tool_sft_history_keeps_tool_call_and_result_together(self):
        items, skipped = constrained_format_from_episode(
            self._tool_episode(),
            "tool_sft",
            max_seq_len=360,
            chars_per_token=4.0,
        )
        self.assertTrue(items, skipped)
        window = items[-1]
        messages = window["messages"]
        self.assertEqual(messages[-1]["role"], "assistant")
        tool_results = [message for message in messages if message.get("role") == "tool"]
        for result in tool_results:
            call_id = result.get("tool_call_id")
            matching_calls = [
                tool_call
                for message in messages
                for tool_call in message.get("tool_calls") or []
                if tool_call.get("id") == call_id
            ]
            self.assertTrue(matching_calls)

    def test_canonical_windows_record_constraint_metadata(self):
        items, skipped = constrained_format_from_episode(
            self._tool_episode(),
            "canonical",
            max_seq_len=256,
            chars_per_token=4.0,
        )
        self.assertTrue(items, skipped)
        for item in items:
            self.assertEqual(item["schema"], "llm-tap.harness_trajectory.v1")
            self.assertLessEqual(item["window"]["estimated_units"], 256)
            self.assertEqual(item["window"]["max_seq_len"], 256)

    def test_sharegpt_window_uses_reasoning_tags(self):
        episode = self._tool_episode()
        episode["messages"][-1]["reasoning"] = "Need a concise final answer."
        items, skipped = constrained_format_from_episode(
            episode,
            "sharegpt",
            max_seq_len=256,
            chars_per_token=4.0,
        )
        self.assertTrue(items, skipped)
        values = [turn["value"] for turn in items[-1]["conversations"]]
        self.assertTrue(any("<reasoning>" in value for value in values))
        self.assertFalse(any("<think>" in value for value in values))


if __name__ == "__main__":
    unittest.main()
