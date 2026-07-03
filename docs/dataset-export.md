# 训练数据导出设计

本文说明 `llm-tap` 如何把代理采集到的手机/客户端 LLM 调用，转换成可用于微调的数据集格式，包括处理原则、各格式差异、长轨迹滑窗方法和脚本参数。

## 数据流

`llm-tap` 的数据处理分三层：

```text
原始调用 JSON
  -> canonical harness trajectory
  -> ShareGPT / tool_sft / OpenAI / OpenAI windowed
```

第一层是采集阶段保存的原始调用。每次调用一个 JSON 文件，包含：

```json
{
  "meta": { "...": "..." },
  "request": { "...": "..." },
  "response": { "...": "..." },
  "headers": { "...": "..." }
}
```

第二层是 `canonical` 中间格式。它不绑定 OpenAI、Anthropic 或任何训练框架，统一表示：

- `message`: system / developer / user / assistant 文本消息
- `tool_call`: assistant 发起的工具调用
- `tool_result`: tool 返回的环境结果
- `reasoning`: Responses API 中独立出现的 reasoning 片段
- `tools`: 请求里的工具定义
- `source` / `harness` / `labels` / `stats`: 溯源和统计信息

第三层才是具体训练格式。这样做的好处是：原始协议只需要解析一次，后续可以继续增加新的导出格式。

## 质量过滤

默认导出会跳过低质量样本：

- 没有 assistant 输出的调用
- 原始文件缺失或不可读的调用
- 不支持的协议

采集阶段也会避免保存明显的业务错误响应。上游如果返回 HTTP 200，但 JSON body 本身表示失败，例如：

```json
{"code":500,"msg":"404 NOT_FOUND","success":false}
```

或顶层存在 `error`、Responses `status=failed/incomplete`、Anthropic 流式 `type=error`，代理会继续把响应原样透传给客户端，但不会把这次调用保存成训练数据。

如果调试时确实想保留低质量样本，可以在导出时加：

```bash
python export_harness_dataset.py export --include-skipped --format canonical --out data/debug.jsonl
```

## 导出格式

### canonical

命令：

```bash
python export_harness_dataset.py export --format canonical --out data/harness.jsonl
```

特点：

- JSONL，每行一个 episode
- 信息保留最完整
- 适合做二次转换、调试、审计
- 不建议直接拿去训练普通 chat 模型

适用场景：

- 以后还要导出其他格式
- 需要检查数据质量
- 需要保留原始协议片段和 harness 信息

### sharegpt

命令：

```bash
python export_harness_dataset.py export --format sharegpt --out data/sharegpt.json
```

特点：

- JSON 数组
- 默认只输出 `id` 和 `conversations`
- 角色映射为 ShareGPT 风格：`human` / `gpt` / `system` / `observation`
- 工具定义会注入到 `system` 消息里的 `<tools>...</tools>`
- 工具调用和工具结果会序列化为 `<tool_call>` / `<tool_result>` 文本块

适用场景：

- 目标框架只吃 ShareGPT 对话格式
- 不需要结构化 function calling 字段

可以关闭工具定义注入：

```bash
python export_harness_dataset.py export --format sharegpt --no-tools --out data/sharegpt.json
```

### tool_sft

命令：

```bash
python export_harness_dataset.py export --format tool_sft --out data/tool_sft.jsonl
```

特点：

- JSONL，每行一个样本
- 顶层保留 `tools`
- `messages` 中保留结构化 `assistant.tool_calls`
- `role=tool` 消息保留 `tool_call_id`
- 可用时保留 `assistant.reasoning_content`

适用场景：

- 训练框架支持结构化工具调用
- 需要让模型学习 function calling 参数生成

### openai

命令：

```bash
python export_harness_dataset.py export --format openai --out data/openai_finetune.jsonl
```

特点：

- JSONL，每行一个 `{"messages":[...]}`
- `developer` 映射为 `system`
- Responses API 的独立 `reasoning` 会合并进下一条 `assistant.content`
- reasoning 使用 `<think>...</think>` 包裹
- 连续工具调用会合并为同一个 `assistant.tool_calls` 数组
- `function.arguments` 始终序列化为 JSON 字符串
- `tool` 消息保留 `tool_call_id`

示例结构：

```json
{
  "messages": [
    {"role": "system", "content": "你是一个具备工具调用能力的助理。"},
    {"role": "user", "content": "帮我检查项目状态。"},
    {
      "role": "assistant",
      "content": "<think>\n需要先查看 git 状态。\n</think>",
      "tool_calls": [
        {
          "id": "call_abc",
          "type": "function",
          "function": {
            "name": "exec_command",
            "arguments": "{\"cmd\":\"git status --short\"}"
          }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_abc",
      "name": "exec_command",
      "content": "## main...origin/main"
    },
    {"role": "assistant", "content": "当前工作区是干净的。"}
  ]
}
```

适用场景：

- OpenAI Chat Completions fine-tuning 数据格式
- LLaMA-Factory / TRL / Axolotl / Unsloth 等支持 OpenAI messages 格式的数据加载器

注意：该格式本身不包含顶层 `tools`。工具调用轨迹在 `messages` 内通过 `assistant.tool_calls` 和 `role=tool` 表示。

### openai_windowed

命令：

```bash
python export_harness_dataset.py export \
  --format openai_windowed \
  --max-seq-len 4096 \
  --out data/openai_windowed.jsonl
```

这是实验性长轨迹滑窗格式。输出仍然是 OpenAI messages JSONL，但一条长 episode 会被切成多条训练样本。

每条 window 的结构：

```text
[固定前缀] + [最近历史] + [当前 assistant 目标]
```

其中：

- 固定前缀：第一个 assistant 之前的 system / user 等消息
- 最近历史：目标 assistant 之前最近的 assistant/tool/user 轨迹
- 当前目标：窗口最后一条 assistant 消息，也是这条样本的训练目标

窗口构造原则：

1. 找出 episode 中每一条可训练的 `assistant` 消息。
2. 以该 assistant 作为 target。
3. 固定保留第一条 assistant 之前的前缀。
4. 从 target 前一条消息开始，按轨迹组倒序塞入最近历史。
5. 如果工具返回太长，保留尾部并加 `"[...truncated...]"` 标记。
6. 如果 system 前缀太长，按 `--prefix-budget-ratio` 压缩 system 内容。
7. 每条输出最后一条消息必须是 assistant。

为什么这么做：

- 长 Agent 轨迹常常超过训练 `cutoff_len`
- 直接截断只会训练开头，学不到后半段修复和决策
- 以 assistant 决策点为目标滑窗，可以让每一步工具调用和最终回复都成为训练目标
- 训练框架通常会对 assistant 之前的上下文做 loss mask，只对 assistant 生成部分计算 loss

`--max-seq-len` 对会话层级的影响：

`--max-seq-len` 越大，每个 window 的预算越多，通常能保留更多最近的会话层级，例如更多组 `assistant -> tool` 历史、更多工具返回内容，以及更少被压缩的 system 前缀。反过来，`--max-seq-len` 越小，导出器越倾向于只保留固定前缀、当前目标 assistant，以及离目标最近的一两轮工具交互。

这意味着：

- 更大的 `--max-seq-len` 会提升上下文完整度，更接近真实长任务运行轨迹。
- 更大的 `--max-seq-len` 会增加训练显存、训练时间和 token 成本。
- 当前长度是启发式估算，不等于目标模型 tokenizer 的真实 token 数。
- `--prefix-budget-ratio` 会限制固定前缀最多占多少预算，避免超长 system prompt 把最近历史全部挤掉。

例如在硬件允许时，可以导出 8192 估算窗口：

```bash
python export_harness_dataset.py export \
  --format openai_windowed \
  --max-seq-len 8192 \
  --out data/openai_windowed_8192.jsonl
```

token 预算说明：

导出器不知道最终被训练模型的 tokenizer，所以不能精确计算 token。当前实现使用启发式估算：

```text
estimated_units = JSON 字符数 / --chars-per-token
```

默认：

```text
--max-seq-len 4096
--chars-per-token 4.0
--prefix-budget-ratio 0.45
```

这不是严格 token 数。训练 Qwen、Llama、Gemma 等模型时，最终是否超过长度仍应以训练框架使用的 tokenizer 为准。

建议：

- 想更保守：调小 `--chars-per-token`，例如 `2.0`
- 想保留更多历史：调大 `--chars-per-token`，例如 `4.5`
- system prompt 太长：调小 `--prefix-budget-ratio`
- 目标模型 tokenizer 明确后，后续可以扩展为 HuggingFace tokenizer 精确估算

## 参数说明

全局参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--db PATH` | `~/.llm-tap/calls.db` | 指定读取的 SQLite 调用数据库。 |

`inspect` 子命令：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--preview N` | `3` | 输出前 N 条 episode 预览。 |
| `--limit N` | 不限制 | 只读取前 N 条调用。 |
| `--window-budget` | 关闭 | 估算 `openai_windowed` 至少需要多大的 `--max-seq-len`，才能保留完整固定前缀和至少一轮 assistant 目标。 |
| `--chars-per-token N` | `4.0` | 配合 `--window-budget` 使用，无 tokenizer 时的字符/token 估算除数。 |

示例：

```bash
python export_harness_dataset.py inspect --preview 5 --limit 100
```

估算窗口最小需求：

```bash
python export_harness_dataset.py inspect --window-budget --preview 5
```

报告中的 `window_budget.recommended_min_max_seq_len` 表示：在当前 `--chars-per-token` 估算口径下，为了完整保留固定前缀，并且至少包含一个当前 assistant 目标轮次，数据集中最难容纳的样本大约需要的 `--max-seq-len`。如果目标 assistant 触发了工具调用，估算会把紧随其后的 tool 结果也算进这一轮。

这个值通常会明显大于实际导出时设置的 `--max-seq-len`，因为 `openai_windowed` 在预算不足时会压缩 system 前缀和长工具输出。它的用途是帮助判断：如果完全不压缩关键前缀和一轮会话，硬件大概要承受多长的窗口。

`export` 子命令：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--out PATH` | 必填 | 输出路径。建议写到 `data/` 下。 |
| `--format FORMAT` | `canonical` | 导出格式：`canonical`、`sharegpt`、`tool_sft`、`openai`、`openai_windowed`。 |
| `--limit N` | 不限制 | 只导出前 N 条调用。 |
| `--include-skipped` | 关闭 | 包含默认会跳过的低质量样本。 |
| `--include-metadata` | 关闭 | 在支持的格式中写入调试/溯源元数据。正式训练通常不需要。 |
| `--no-tools` | 关闭 | 仅对 `sharegpt` 生效，不注入 `<tools>...</tools>` 工具定义。 |
| `--max-seq-len N` | `4096` | 仅对 `openai_windowed` 生效，窗口最大估算长度。 |
| `--chars-per-token N` | `4.0` | 仅对 `openai_windowed` 生效，无 tokenizer 时的字符/token 估算除数。 |
| `--prefix-budget-ratio N` | `0.45` | 仅对 `openai_windowed` 生效，固定前缀最多占窗口预算的比例。 |

## 常用命令

检查数据：

```bash
python export_harness_dataset.py inspect --preview 3
```

导出普通 OpenAI 格式：

```bash
python export_harness_dataset.py export \
  --format openai \
  --out data/openai_finetune.jsonl
```

导出 4096 估算窗口的 OpenAI 滑窗格式：

```bash
python export_harness_dataset.py export \
  --format openai_windowed \
  --max-seq-len 4096 \
  --out data/openai_windowed.jsonl
```

导出更保守的 4096 滑窗格式：

```bash
python export_harness_dataset.py export \
  --format openai_windowed \
  --max-seq-len 4096 \
  --chars-per-token 2.0 \
  --out data/openai_windowed_conservative.jsonl
```

导出带 metadata 的调试样本：

```bash
python export_harness_dataset.py export \
  --format openai_windowed \
  --include-metadata \
  --limit 5 \
  --out data/debug_windowed.jsonl
```

读取当前目录开发数据库：

```bash
python export_harness_dataset.py --db calls.db inspect
```

## 训练注意事项

1. `data/` 默认不进 Git，导出的训练数据不要提交到仓库。
2. `openai_windowed` 的长度是估算值，不是目标模型真实 token 数。
3. 训练前最好用目标框架和目标 tokenizer 再做一次长度统计。
4. 如果训练框架不会自动只对 assistant 计算 loss，需要显式配置 loss mask。
5. 工具返回内容通常很长，训练时不应对 `role=tool` 计算 loss。
6. 对真实用户数据训练前，应先做脱敏和隐私检查。
