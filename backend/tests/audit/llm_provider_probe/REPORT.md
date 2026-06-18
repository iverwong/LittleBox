# LLM Provider 调用链路对账报告

> 生成时间：2026-05-19 UTC  
> 测试范围：58 个探针用例 × 3 层（L1 httpx / L2 OpenAI SDK / L3 LangChain）  
> 端点：DS-native (`https://api.deepseek.com`) | BL-compat (`https://dashscope.aliyuncs.com/compatible-mode/v1`)  
> 模型：统一 `deepseek-v4-flash`（百炼侧部署版本待确认，实测接口兼容）  
> 运行：全部 58 通过，覆盖 F1-F7 七个调用形态

---

## A. 端点字段差异矩阵

| 字段 | DS-native 行为 | BL-compat 行为 | ChatDeepSeek@DS 实发 | ChatDeepSeek@BL 实发 | ChatOpenAI@DS 实发 | ChatOpenAI@BL 实发 |
|---|---|---|---|---|---|---|
| `model` | `deepseek-v4-flash` | `deepseek-v4-flash` | 同左 | 同左 | 同左 | 同左 |
| `tool_choice="auto"` | 正常工作 | 正常工作 | 原样"auto" | 原样"auto" | 原样"auto" | 原样"auto" |
| `tool_choice="required"`(="any"映射) | **400 rejected** ❌ | 正常工作 | 400 ❌ | 正常 ✅ | 400 ❌ | 正常 ✅ |
| `tool_choice={function object}` | **400 rejected** ❌ | **400 rejected** ❌ | 400 ❌ | 400 ❌ | 400 ❌ | 400 ❌ |
| `thinking` 字段 | 原生支持 | 兼容支持 | 同左 | 同左 | 同左 | 同左 |
| `enable_thinking` extra_body | N/A | 不需要（thinking 字段即可） | N/A | N/A | N/A | N/A |
| `strict` | 未发送（默认不加） | 未发送（默认不加） | 未发送 | 未发送 | 未发送 | 未发送 |
| `response_format` | 未测试 | 未测试 | 未测试 | 未测试 | 未测试 | 未测试 |
| `stream_options.include_usage` | 有效 | 有效 | 有效 | 有效 | 有效 | 有效 |
| `reasoning_content` 字段路径 | 顶层 `delta.reasoning_content` | 同左 | `additional_kwargs.reasoning_content` | 同左 | 无 reasoning_content | 同左 |
| `finish_reason`（流式末 chunk） | `choices[0].finish_reason` | 同左 | `additional_kwargs.response_metadata.finish_reason` 不存在，必须从独立追踪获得 | 同左 | 同左 | 同左 |

---

## B. tool_choice="any" 在两源四组合下的真实映射

| 组合 | LangChain tool_choice | HTTP body 实发 tool_choice | API 响应 | 备注 |
|---|---|---|---|---|
| ChatDeepSeek @ DS | `"any"` | `"required"` | 400 `deepseek-reasoner does not support this tool_choice` | DS v4 (reasoner) 拒绝非 auto 的 tool_choice |
| ChatOpenAI @ DS | `"any"` | `"required"` | 400 同上 | 同上（同为 DS 后端） |
| ChatDeepSeek @ BL | `"any"` | `"required"` | 200 + tool_calls ✅ | BL 端 `required` 可用 |
| ChatOpenAI @ BL | `"any"` | `"required"` | 200 + tool_calls ✅ | 同上 |

**F3 (auto)**: 全部四组合正常通过，模型返回 content + reasoning_content（但 F3 auto 下模型未返回 tool_calls，因为 prompt 在 auto 下可以只做文本回复）。

**F4 (specific function)**: 全部四组合均失败——DS 端 400（reasoner 不支持），BL 端 400（`tool_choice parameter does not support being set to required or object in thinking mode`）。

**结论**: 在 DS-native 端（deepseek-v4-flash / deepseek-reasoner），`tool_choice` 只允许 `"auto"`，不允许任何形式的强制选择（`"required"` 或函数对象）。BL 端允许 `"required"` 但同样不允许函数对象形式。

---

## C. 思考模式开关 × 字段路径

### C1 实发 body / 响应 reasoning_content / reasoning_tokens

| L1 | thinking=disabled || thinking=enabled || notset ||
|---|---|---|---|---|---|
| | rc | rt_tokens | rc | rt_tokens | rc | rt_tokens |
| DS-native | no | 0 | YES | 91 | YES | 115 |
| BL-compat | no | 0 | YES | 141 | YES | 120 |

**关键发现**：
- 两端均正确识别 `thinking={"type":"disabled"}` 字段：reasoning_content 完全消失，reasoning_tokens 归零
- 两端默认（不传 thinking 字段）：reasoning_content 出现（v4 默认开思考）
- `reasoning_tokens` 记录在 `completion_tokens_details.reasoning_tokens`（OpenAI 协议标准字段）
- BL 端对 DeepSeek 模型使用 `thinking` 字段（DS 原生语法）工作正常，**不需要** `enable_thinking` 额外字段

### C2 LangChain 透传路径

| 包装 | 传参方式 | 实发 body 中 thinking 字段 | 生效 |
|---|---|---|---|
| ChatDeepSeek | `extra_body={"thinking":{"type":"disabled"}}` | `thinking: {type: disabled}` | ✅ |
| ChatOpenAI | `extra_body={"thinking":{"type":"disabled"}}` | `thinking: {type: disabled}` | ✅ |

**结论**：两包装均通过 `extra_body` 正确透传 thinking 字段。`model_kwargs` 路径未测试（未发现使用 `model_kwargs` 的必要性，`extra_body` 已解决问题）。

---

## D. ChatDeepSeek 跨端可行性 / ChatOpenAI 跨端可行性

### ChatDeepSeek(api_base=BL_URL) 能否跑通？

**可以 ✅**

实测组合 "BL-CDS"（ChatDeepSeek @ 百炼）在 F1/F2/F3/F5 全部通过。注意：
- `api_base=` 接收百炼的 `https://dashscope.aliyuncs.com/compatible-mode/v1`，底层 SDK 构造 URL 为 `.../v1/chat/completions`
- `reasoning_content` 通过 `additional_kwargs.reasoning_content` 可获得
- tool_choice 行为与 BL 端标一致（`auto` ✅ / `required` ✅ / function 对象 ❌）

### ChatOpenAI(base_url=DS_URL) 能否跑通？

**可以 ✅**

实测组合 "DS-COAI"（ChatOpenAI @ DeepSeek）在 F1/F3/F5 全部通过。注意：
- `base_url="https://api.deepseek.com"` 正常工作
- **`reasoning_content` 在 ChatOpenAI 下消失**：`additional_kwargs` 中不含 `reasoning_content` 字段，因为 `ChatOpenAI` 不做 `reasoning_content` 字段提升（这是 `ChatDeepSeek` 独有的增强）
- tool_choice 行为与 DS 端一致（只有 `auto` 可用）

**核心差异**：ChatDeepSeek 从 `delta.reasoning_content` 提升到 `additional_kwargs`，而 ChatOpenAI 不做此提升。如果要跨端统一使用 ChatOpenAI，需从原始 `chunk.model_dump()` 中手动提取 `reasoning_content`。

---

## E. with_fallbacks / with_retry 行为

### F6 with_fallbacks

| 项目 | 结果 |
|---|---|
| 主 provider | ChatDeepSeek @ DS（mock httpx.ConnectError） |
| 备 provider | ChatDeepSeek @ BL（mock 200） |
| 主端请求次数 | **3 次**（OpenAI SDK 内置 max_retries=2 触发重试后失败） |
| 备端请求次数 | **1 次**（fallback 生效） |
| 响应内容 | ✅ 正确返回 mock 内容 |
| 结论 | with_fallbacks 机制正常工作；注意 SDK 内置重试会增加主端失败前的等待时间 |

### F7 with_retry

| 项目 | 结果 |
|---|---|
| 第一次请求 | 429 RateLimit（mock） |
| 第二次请求 | 200（mock） |
| 实际请求次数 | **2 次**（1 原始 + 1 重试） |
| 响应内容 | ✅ 正确返回 mock 内容 |
| body 一致性 | 两次请求 body 完全相同（messages/tools/model 一致） |
| 结论 | with_retry 正常工作，429 触发指数退避重试 |

---

## F. 项目 LLM 调用面 × 包装能力交叉表

| 调用形态 | 单 ChatOpenAI | 单 ChatDeepSeek | 双包装（现状） | 自建 LLMProvider 抽象 |
|---|---|---|---|---|
| **F1 流式 + 无工具** | ✅ 可跑通，但 reasoning_content 丢失 | ✅ 可跑通 | ✅ | ✅ |
| **F2 非流式 + any** | ❌ DS 端 400（required 不支持） | ❌ DS 端 400 | ❌ DS 端 400（注1） | ❌ DS 端 400（注1） |
| **F3 非流式 + auto** | ✅ | ✅ | ✅ | ✅ |
| **F4 指定 function** | ❌ 两源均 400 | ❌ 两源均 400 | ❌ 两源均 400 | ❌ 两源均 400 |
| **F5 思考模式开/关** | ✅ extra_body 透传 | ✅ extra_body 透传 | ✅ | ✅ |
| **F6 with_fallbacks** | — | ✅（本探针验证 ChatDeepSeek） | ✅ | — |
| **F7 with_retry** | — | ✅ | ✅ | — |

注1：DS-native 端 `tool_choice="any"` 映射为 `"required"` 后，DeepSeek reasoner 模型返回 400。**这不是包装层的问题，而是 DS API 的限制**。BL 端跑通。

### 关键结论

1. **F4（指定 function）在所有组合下均不可用**。DS 端 `deepseek-reasoner does not support this tool_choice`，BL 端 `tool_choice does not support being set to required or object in thinking mode`。这需要 M9 设计替代方案（例如 post-processing 过滤 tool_calls）。
2. **F2（any/required）仅在 BL 端可用**，DS 端不支持任何强制性 tool_choice。
3. **F1 的 reasoning_content 路径在 ChatOpenAI 下缺失**，这是 ChatDeepSeek 包装的核心价值。
4. **F3（auto）在所有组合下均稳定可用**。

---

## G. 架构决策建议

### 方案 A：回到单 ChatOpenAI 包装 + provider registry 切 base_url + extra_body 注入

| 维度 | 评估 |
|---|---|
| reasoning_content | ❌ 丢失。须改`llm.py`或调用方从原始响应中手动提取 |
| compatibility | ✅ 两源均可跑通 |
| tool_choice | DS 端 `any` 不可用（非包装问题） |
| 迁移成本 | 中。改 `factory.py` + 查所有 `astream` 调用方 |
| **推荐度** | ⚠️ 仅当 reasoning_content 不需要时可选 |

### 方案 B：保留 ChatDeepSeek + ChatOpenAI 双包装（现状）

| 维度 | 评估 |
|---|---|
| reasoning_content | ✅ DS 端通过 ChatDeepSeek 获完整 |
| compatibility | ✅ 两包装各适配对应源 |
| 复杂度 | ⚠️ 双包装 + 条件构造，`factory.py` 已经在管理 |
| **推荐度** | ✅ **推荐**。现状已有 provider registry 抽象，仅需解决 M8/M9 工具兼容性问题 |

### 方案 C：自建 LLMProvider 抽象层

| 维度 | 评估 |
|---|---|
| 维护成本 | ❌ 高。自定义 astream/ainvoke 需大量胶水代码 |
| 灵活性 | ✅ 可完全控制 tool_choice 映射、reasoning 提取 |
| 必要性 | ⚠️ 在可预见的调用形态下，LangChain + 双包装已覆盖 |
| **推荐度** | ❌ 不推荐。过度工程化，当前问题双包装都能解决 |

### 推荐方案 B + 改进项

**需改动文件清单**（`backend/app/domain/chat/`）：

| 文件 | 改动 | 估计行数 |
|---|---|---|
| `factory.py` | `_build_chat_deepseek` 中 `extra_body` 增加 `reasoning_effort` 条件控制 | ~5 |
| `factory.py` | `_build_chat_openai` 增加 `thinking` 模式控制能力（通过 `extra_body`） | ~5 |
| `factory.py` | `_PROVIDER_REGISTRY` 中注册两 provider 的完整参数 | ~3 |
| `llm.py` | （已不存在，迁移完成） | 0 |
| `graph.py` | `call_main_llm` 中两包装的条件构造逻辑 | ~10 |
| `graph.py` | F4 兜底策略：auto 模式下对输出做 tool_call 过滤，替代 specific function choice | ~15 |
| **总计** | | **~38 行** |

---

## H. 对架构记录的回写建议

以下条目需更新到架构文档 §13.1 / §13.4：

### §13.1 需订正
- **thinking 字段路径**: 当前记录 "chunk.response_metadata 顶层为空，真路径在 chunk.additional_kwargs['response_metadata']['finish_reason']" —— 实测 `additional_kwargs.response_metadata` 不存在 `finish_reason`。`finish_reason` 仅出现在 L1/L2 末 chunk 的原始响应中；L3 无法从单个 chunk 的 `additional_kwargs` 直接获取 `finish_reason`，需要累计到末 chunk 通过 `chunk.usage_metadata` 的有无来判断是否末 chunk，或在调用方自行追踪。
- **正确关闭思考的写法**: 追加明确说明 —— `extra_body={"thinking":{"type":"disabled"}}` 两源均有效。`enable_thinking=False` 不需要。注意：`ChatDeepSeek(..., model_kwargs={"thinking":...})` 路径未验证有效性，优先用 `extra_body`。
- **tool_choice 限制**: 追加说明 "DS-native deepseek-reasoner 仅支持 tool_choice='auto'，拒绝 required/any/function"。BL 端支持 auto 和 required，拒绝 function 对象。

### §13.4 需订正
- **F4 方案调整**: M9 "强制 audit_output 兜底"不能通过 `tool_choice={"type":"function","function":{"name":"audit_output"}}` 实现，因为两源均拒绝。应改用 post-processing 过滤：调用 `tool_choice="auto"`，在响应中过滤出需要的 tool_call，或设置 `stop_after_n_tool_calls`。

---

## I. 对 M8 D11 v2 决策（bind_tools tool_choice="any"）的复核结论

**复核结论：需要修订。**

当前 D11 v2 使用 `bind_tools(tool_choice="any")` 在 DS-native 端（`deepseek-reasoner`）会收到 HTTP 400，因为 DeepSeek reasoner 模型不支持 `tool_choice="required"`（即 "any" 的真实映射）。

**修订建议**：将 M8 审计 LLM 的 `tool_choice` 从 `"any"` 改为 `"auto"`。原因是：
1. auto 模式在 DS-native 和 BL-compat 两端均稳定工作
2. 对于 M8 审查场景，auto 模式下模型可以自行决定是否调工具以及调哪个工具
3. 如果需要确保 audit_output 被调用，可以在图层面做约束（例如在 agentic loop 中检查输出是否包含必要的 tool_call，若没有则提示模型继续）
4. DS 不支持的任何强制 tool_choice 会让 BL 侧也相应受损；改用 auto 统一两端的兼容层

**新的写法和迁移路径**：
```python
# 当前 (D11 v2)
llm.bind_tools(tools, tool_choice="any")

# 建议
llm.bind_tools(tools, tool_choice="auto")
```

**注意**：如果 M8 业务逻辑要求"每一次都必须调至少一个工具"，不应靠 `tool_choice="any"`（DS 不支持），而应：
1. 先用 `tool_choice="auto"` 调用
2. 检查响应中 tool_calls 是否为空
3. 若为空，用包含"请必须选择一个工具"指令的追问再做一次调用
4. 或在 system prompt 中明确要求"每次回复都必须调用一个工具"

---

## 附录：Artifact 清单

共 58 个 artifact JSON 文件，位于 `artifacts/` 目录。命名规范：

```
{layer}_{provider}_{wrapper}_{Fn}_{thinking_mode}.json
```

- L1 `httpx` 14 个，L2 `AsyncOpenAI` 14 个，L3 `ChatDeepSeek`/`ChatOpenAI` 30 个
- F1-F4 的 thinking_mode = "enabled"，F5 为三态 ("enabled"/"disabled"/"notset")
- F6/F7 的 provider 字段为 "fallback-test"/"retry-test"

每个 artifact 含完整 `request.body`、`response.body`、`parsed_output`、`_req_timeline`（请求时间线）。

---

## 补充验证 v3

> 基于 5 组补充探针（45 个新测试用例）的追加验证。测试时间：2026-05-19 UTC
> 全部 45/45 通过，覆盖补1'~补5。

---

### 补1'：思考模式 + 强制工具的多层多写法穷尽

**矩阵**：2 端点 × 2 tool_choice × (7 L1 变体 + 1 L2 + 1 L3) = 36 用例

| 端点 | tool_choice | 所有变体 (v0~v6 + L2 + L3) | 结论 |
|---|---|---|---|
| DS-native | `"required"` | 全部 400 ❌ | **不可用**。所有变体均返回 `deepseek-reasoner does not support this tool_choice` |
| DS-native | `{function object}` | 全部 400 ❌ | **不可用**。同上 |
| BL-compat | `"required"` | **全部 200** ✅ | **可用**。v0~v6、L2、L3 全部返回 tool_calls |
| BL-compat | `{function object}` | 全部 400 ❌ | **不可用**。返回 `tool_choice does not support being set to required or object in thinking mode` |

**尝试过的 L1 变体**（全部无效于 DS-native）：
- v0: 基线 body
- v1: + `reasoning_effort="high"`
- v2: + `strict=true` 在 function 定义中
- v3: + `parallel_tool_calls=false`
- v4: + system prompt 显式约束
- v5: v1 + v4
- v6: v1 + v2 + v3 + v4（全堆）

**结论**：DeepSeek reasoner 模型在 thinking 模式下**确实不支持**任何形式的强制 tool_choice（`"required"` 或 `{function object}`）。这是 API 层面的硬限制，不是包装问题。L3 ChatDeepSeek 的行为与 L1 httpx 原生一致，没有破坏性转换。

**最小可用 body 模板（BL 端 required 可用）**：
```json
{
  "model": "deepseek-v4-flash",
  "messages": [{"role": "user", "content": "..."}],
  "tools": [{"type":"function","function":{...}}],
  "tool_choice": "required",
  "thinking": {"type": "enabled"},
  "stream": false
}
```

**修订 D11 v2 决策**：「thinking 模式下强制 tool_choice」在 DS 端不可行，M8 审查 LLM 必须改为 `tool_choice="auto"` + post-processing 约束（检查输出是否含目标 tool_call，不含则追问）。

---

### 补2：finish_reason 末 chunk 真路径

**验证结果**：架构 §13.2 的路径记录**有误**。

| 路径 | 实测值 | 结论 |
|---|---|---|
| `chunk.additional_kwargs["response_metadata"]["finish_reason"]` (extractors.py 当前路径) | chunk #148: `None` | ❌ 错误路径，返回 None |
| `chunk.response_metadata["finish_reason"]` (直接属性) | chunk #148: `"stop"` | ✅ **真路径** |
| `chunk.usage_metadata` | 末 chunk: `None` | ❌ 末 chunk 无 usage_metadata（与堆栈追踪不符） |

**详细发现**（基于 150-chunk 流式输出的末 5 chunk）：

```
Chunk #145-147: additional_kwargs={}, response_metadata={"model_provider": "deepseek"}
Chunk #148: additional_kwargs={}, response_metadata={"finish_reason": "stop", "model_name": "deepseek-v4-flash", "system_fingerprint": "...", "model_provider": "deepseek"}
Chunk #149: additional_kwargs={}, response_metadata={}
```

**结论**：
- `finish_reason` 出现在**倒数第二个 chunk**（#148）的 **`chunk.response_metadata["finish_reason"]`** 直接属性中
- `chunk.additional_kwargs` 在末 5 chunk 中全部为 `{}`，`additional_kwargs["response_metadata"]` 不存在
- `chunk.usage_metadata` 在所有 chunk 中均为 `None`（与 extractors.py 注释"末帧由 SDK 自动设置"不符）

**修复建议**（extractors.py）：
```python
def extract_finish_reason(chunk: AIMessageChunk, provider: str) -> str | None:
    # 当前路径（错误）:
    # ak = chunk.additional_kwargs or {}
    # metadata = ak.get("response_metadata") or {}
    
    # 真路径:
    metadata = chunk.response_metadata or {}
    fr = metadata.get("finish_reason")
    return fr if fr in ALLOWED_FINISH_REASONS else None
```

---

### 补3：factory.py max_retries 覆写检查

| 检查项 | 结果 |
|---|---|
| factory.py `_build_chat_deepseek` 是否传 `max_retries` | ❌ 未传（source 中无 "max_retries"） |
| factory.py `_build_chat_openai` 是否传 `max_retries` | ❌ 未传 |
| OpenAI SDK 默认 `max_retries` 值 | **2** |
| `max_retries=2` 时 F6 mock 主端调用次数 | **3 次**（1 原始 + 2 SDK 重试） |
| `max_retries=0` 时 F6 mock 主端调用次数 | **1 次** |

**生产建议**：应固定 `max_retries=0`，理由：
1. factory.py 已有 `with_retry()` + `stop_after_attempt=3` 显式重试策略（捕获 `RateLimitError`/`APITimeoutError`/`APIConnectionError`）
2. OpenAI SDK 内置 `max_retries=2` 的退避策略与 LangChain `with_retry()` 退避策略**叠加**，导致瞬态错误时主端被调 3×SDK 内置 + 3×LangChain with_retry = 9 次尝试才进入 fallback
3. 设置 `max_retries=0` 让 SDK 层不重试，所有重试逻辑由 LangChain `with_retry()` 统一管理

**修改位置**：`factory.py` `_build_chat_deepseek()` 和 `_build_chat_openai()` 增加 `max_retries=0`。

```python
# _build_chat_deepseek 新增:
return ChatDeepSeek(
    ...
    max_retries=0,  # SDK 内置重试交由 LangChain with_retry 统一管理
)
```

---

### 补4：多轮 agentic loop reasoning_content 回传（致命空白）

| 层 | 第二轮 HTTP body 中 assistant message 的 reasoning_content | 结果 |
|---|---|---|
| L1 (httpx 原生) | ✅ **存在**（54 chars） | 正确保留 |
| L2 (OpenAI SDK) | ✅ 验证通过 | 正确保留 |
| L3 (LangChain ChatDeepSeek) | ❌ **不存在**（key 缺失） | **序列化丢失** |

**根因**：`ChatDeepSeek` 继承自 `BaseChatOpenAI`，**未覆写** `_convert_message_to_dict` 方法。langchain-openai 基类的 `_convert_message_to_dict` 在将 `AIMessage` 转换为 API 请求 dict 时，**丢弃了所有 `additional_kwargs`**，其中包括 `reasoning_content`。

```python
# langchain-openai 的 _convert_message_to_dict 输出（已确认）:
{
    "content": null,
    "role": "assistant",
    "tool_calls": [...]  # 工具调用保留
    # reasoning_content 消失了！
}
```

**影响**：多轮 agentic loop（如 M8 审查的 tool-use 循环）中，第二轮及之后的 LLM 请求不会包含上一轮模型的 thinking 链，导致：
1. 模型无法「基于上一轮的推理继续推理」，失去思考连续性
2. 对于需要多步工具调用的场景，模型表现逐轮退化

**修复方案（三选一）**：

**方案 A（推荐，~5 行）**：monkeypatch langchain-openai 的 `_convert_message_to_dict`：

```python
# 在 factory.py 或 app/__init__.py 加载时执行
from langchain_core.messages import AIMessage
import langchain_openai.chat_models.base as lcoai

_original_convert = lcoai._convert_message_to_dict

def _patched_convert(message, *args, **kwargs):
    result = _original_convert(message, *args, **kwargs)
    if isinstance(message, AIMessage):
        rc = message.additional_kwargs.get("reasoning_content")
        if rc:
            result["reasoning_content"] = rc
    return result

lcoai._convert_message_to_dict = _patched_convert
```

**方案 B**：升级/PR 到 `langchain-deepseek`，在 `ChatDeepSeek` 类中覆写 `_convert_message_to_dict`。这是上游修复。

**方案 C**：在使用 L3 多轮调用时，不在 LangChain 层面做消息管理，改为手动构造消息 dict 传给底层 OpenAI SDK。不建议（破坏了使用 LangChain 的意义）。

---

### 补5：reasoning_effort high vs max 对比

| 指标 | `reasoning_effort="high"` | `reasoning_effort="max"` | 差异 |
|---|---|---|---|
| reasoning_tokens | 280 | 471 | **+68%** |
| reasoning_content 长度 | 481 chars | 781 chars | **+62%** |
| 总 tokens | 1478 | 1771 | +20% |
| 延迟 | 26.59s | 32.31s | +22% |
| finish_reason | stop | stop | 正常 |

**结论**：
- `reasoning_effort="max"` 被 DS API **正常接受并生效**，不是无效值
- "max" 模式产生约 **68% 更多的 reasoning_tokens**，代价是 22% 更长的延迟
- M8 D2 决策 `audit_reasoning_effort="max"` **应保留**，因为审查场景需要最深度的推理
- 主对话（M6）可以使用 `"high"` 以节省推理时间

**DS 私有扩展验证**：
- `reasoning_effort` **不是** OpenAI 标准参数（OpenAI 使用 `reasoning_effort: "low"|"medium"|"high"`）
- DeepSeek 接受 `"high"` 和 `"max"`，文档提到 `"xhigh"` 自动映射为 `"max"`
- `"low"` / `"medium"` 自动映射为 `"high"`（按 §1.1 文档）（未实测）

---

### 补充验证汇总：影响生产代码的 bug 清单

| 优先级 | 问题 | 影响模块 | 修复方案 |
|---|---|---|---|
| **P0** | `extract_finish_reason` 读错路径 | `extractors.py:37-42` | `chunk.additional_kwargs["response_metadata"]` → `chunk.response_metadata` |
| **P0** | `reasoning_content` 多轮序列化丢失 | LangChain `_convert_message_to_dict` | monkeypatch 或上游 PR |
| **P1** | `max_retries` 未设 0，SDK 内置重试与 `with_retry` 叠加 | `factory.py` | 两 builder 增加 `max_retries=0` |
| **P2** | M8 D11 `tool_choice="any"` 在 DS 端必定 400 | M8 audit LLM | 改为 `"auto"` + post-processing |
| **P3** | `reasoning_effort="max"` 已确认可用 | 架构文档 | 保留 M8 D2 决策 |

> 全部补测试的 artifact JSON 位于 `artifacts/` 目录，按 `F2prime_*`、`F1-finish-reason-末chunk.json`、`F6-maxretries-*`、`F8*`、`F9-*` 命名。

