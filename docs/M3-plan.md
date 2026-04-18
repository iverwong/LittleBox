# M3 · Streaming 链路验证 — 实施计划 (3/17)

## 目标概述

打通"用户发消息 → qwen3.5-flash 流式生成 → FastAPI SSE 推送 → Expo RN 客户端逐字渲染"的最小端到端链路，**仅做技术验证，不做产品化**。产出一个可复跑的 Demo 页面，用于验证 qwen3.5-flash 流式延迟、LangGraph `.astream_events()` 与 SSE 管道的稳定性、RN 端 SSE 消费方案，以及流式中断的收尾正确性。通过与否将决定后续 M6（主对话图）是否沿用此技术方案。

**不做**：账号鉴权 / 消息持久化 / 滑动窗口上下文 / 动态系统 prompt / 审查 pipeline / 正式聊天 UI（均甩给 M4-M9）。本里程碑 LangGraph 图只有一个 `call_main_llm` 节点，系统 prompt 硬编码为一句占位话术。

---

## 技术版本基线

| 技术栈 / 决策项 | 版本 / 结论 | 说明 | LLM 模型 | `qwen3.5-flash`（稳定版 = qwen3.5-flash-2026-02-23） | Qwen3 系列，1M 上下文；M3 **关闭思考模式**（`enable_thinking=false`），避免 reasoning_content 干扰首 token 延迟验证 |
| --- | --- | --- | --- | --- | --- |
| LLM 接入渠道 | 阿里云百炼（Model Studio）OpenAI 兼容接口 | `base_url = https://dashscope.aliyuncs.com/compatible-mode/v1` | LLM 客户端封装 | 自写 `ChatDashScopeQwen(BaseChatModel)`  • 阿里官方 `dashscope` SDK | 走 DashScope 原生端而非百炼 OpenAI 兼容端；SDK 官方维护、Qwen 新特性首发通道；薄包装（~120 行）只做消息格式 / usage / finish_reason 翻译，保留 LangChain 生态（astream_events / bind_tools / with_structured_output / ToolNode / MCP adapter） |
| 已排除方案 | `langchain-qwq.ChatQwen` / `langchain-community.ChatTongyi` / `langchain-openai.ChatOpenAI` 兼容端 | `langchain-qwq` 在 M3 Step 3 实测百炼兼容端 401；前两者 GitHub 长期缺维护、Qwen3 新特性跟进慢；兼容端对内置工具（code_interpreter / web_search / MCP）支持不完整，长期需切 Responses API | 编排 | LangGraph `StateGraph`  • `.astream_events(version="v2")` | 透传 `on_chat_model_stream` 事件到 SSE |
| SSE 传输 | FastAPI `StreamingResponse`，`Content-Type: text/event-stream` | 不引入第三方 sse 库，手写 generator 足够 | SSE 消息协议 | JSON 载荷 + 事件类型 `start / delta / end / error` | 每条 `data: {"type":..., "content":...}\n\n`，结束发一条 `end` 事件（不用裸 `[DONE]`） |
| RN SSE 客户端 | `react-native-sse` 社区包 | API 接近原生 EventSource，支持自定义 headers、断开、错误事件 | Demo 入口 | Expo 应用内 `app/dev-chat.tsx`（顶层路由，不走角色守卫） | M6/M7 上线后清理 |
| API Key | `DASHSCOPE_API_KEY` 环境变量 | 写入 `.env`，通过 `pydantic-settings` 读取（以 `SecretStr` 类型） | Python | 3.14.x（沿用 M1 基线） | `dashscope` SDK 对 3.14 兼容性需在 Step 1 实测中确认 |
| dashscope | 按 M3 Step 1 实测锁定具体版本号（不写 latest）；**最低版本 1.25.2**（多模态异步调用推荐基线，最新 1.25.16） | 阿里官方 Python SDK。**qwen3.5-flash / qwen3.6-plus 均为原生多模态模型**，必须走多模态接口（包纯文本请求同理）——否则报 `400 InvalidParameter: url error`。异步调用走 `from dashscope.aigc.multimodal_conversation import AioMultiModalConversation`（import 路径官方文档未公开示例，按 SDK 源码 + agentscope 社区实践推断，Step 1 裸调确认）；`await AioMultiModalConversation.call(..., stream=True, incremental_output=True)`；**消息 `content` 必须是 `list[dict]`**（纯文本也要包 `[{"text": "..."}]`）。SDK 中国区（华北 2 北京）无需配置 `base_url`；流式 chunk 语义 / `usage` 字段位置需实测后记入偏差 | langchain / langgraph | 最新 pip 版本 | M1 未安装，本里程碑首次引入 |

---

## 关键架构决策

### SSE 事件协议

统一 JSON 包装，便于后续扩展（M6 会增加 `turn_id`、M9 会增加干预类型字段）。

```json
{"type": "start",  "session_id": "<uuid>"}
{"type": "delta",  "content": "你"}
{"type": "delta",  "content": "好"}
{"type": "end",    "finish_reason": "stop"}
{"type": "error",  "message": "...", "code": "upstream_timeout"}
```

选择理由：

- 裸文本流（`data: 你好\n\n`）后续扩展会破坏协议；直接上 JSON 一步到位。
- 使用自定义事件类型而非 SSE 的 `event:` 字段，理由：客户端侧 JSON 解析一次统一处理，少一层分支；后续要加新类型时不需动 SSE 解析层。
- `end` 显式区分于 `error`，便于客户端状态机处理（成功 / 失败走不同 UI）。

### LangGraph 图结构（M3 版）

只有一个节点 `call_main_llm`，State 只含 `messages: list[BaseMessage]`。

选择理由：

- 验证的是**管道**，不是图结构。M6 会把这个图扩成完整的主对话图（load_audit_state / route_by_risk / persist_turn / enqueue_audit 等），但图骨架的流式接入方式在 M3 必须先跑通。
- 使用 `.astream_events(version="v2")` 而非 `.astream()`：前者能拿到 `on_chat_model_stream` 细粒度 token 事件，后者在多节点图里只能拿到节点级 state 快照，不适合做流式转发。

### 中断语义

客户端断开 / 显式 stop 均走同一条路径：**FastAPI 侧检测到 `request.is_disconnected()` → 关闭 LangGraph 异步生成器 → Qwen HTTP 连接释放**。

- 不在服务端维护"stop 信号"状态，客户端点击停止 = 客户端主动 `close()` SSE 连接，由断连检测触发清理。
- 这样设计的好处：无状态，天然支持多实例部署；服务端逻辑只需一条路径处理中断。

### Demo 入口清理契约

- 路由文件：`mobile/app/dev-chat.tsx`
- 后端路由：`backend/app/api/dev_chat.py`，挂在 `/api/dev/*` 前缀下
- **清理时机**：M7（聊天界面正式版）合并时一并删除；计划页在 `2.4 执行偏差记录` 风格位置预埋"清理清单"章节提醒。

---

## 版本核验（Step 0 前置阅读）

| 项 | 需核验 | 失败回退 |
| --- | --- | --- |
| `dashscope >= 1.25.2` 在 Python 3.14 下 `pip install` 成功；`from dashscope.aigc.multimodal_conversation import AioMultiModalConversation` 可导入（主路径裸调测通，如 import 路径不符按实测结果记入偏差） | Step 1 | 无回退——兼容端 / langchain-qwq / ChatTongyi 均已排除；若 SDK 不兼容回到上下文重新选型 |
| `ChatDashScopeQwen.ainvoke()` 能正常返回 `AIMessage`，`usage_metadata.input_tokens > 0`，`finish_reason ∈ {stop, length}` | Step 1 | 先裸调 `AioMultiModalConversation.call()`（消息 `content` 包 `[{"text": "..."}]`）诊断 SDK 链路，再排查薄包装 |
| `ChatDashScopeQwen` 通过 LangGraph `.astream_events(version="v2")` 产出 `on_chat_model_stream` 事件；`incremental_output=True` 下 chunk 为增量而非累计；`llm.disable_streaming is False`（保护流式路径） | Step 2 | 先裸调 `AioMultiModalConversation.call(stream=True, incremental_output=True)` 确认 chunk 语义 + usage 位置 |
| qwen3.5-flash 首 content token 延迟 < 2s（境内网络，`enable_thinking=False`） | Step 1 | 三类 prompt（极简/中等/复杂）各 3 次实测。**实测结果：0.40s / 0.41s / 0.43s（均 < 1s）**，阀值轻松达标；全量数据见 [M3 执行偏差记录](https://www.notion.so/M3-0b31497d177d44d088989220915e33c4?pvs=21) 偏差 3.3。注：若薄包装未显式传 `enable_thinking=False`，模型默认 `True` 会走 reasoning 路径（实测 42.98s），绝对不达标 |

---

## 文件结构

本里程碑新增 / 改动：

```jsx
backend/
├── app/
│   ├── chat/                      # M1 占位，M3 首次填充
│   │   ├── __init__.py
│   │   ├── llm.py                 # ChatDashScopeQwen 构造器（单例）
│   │   ├── dashscope_chat.py      # 自写 ChatDashScopeQwen(BaseChatModel) 薄包装
│   │   ├── graph.py               # LangGraph 单节点图
│   │   └── sse.py                 # SSE 事件序列化 + 生成器
│   ├── api/
│   │   └── dev_chat.py            # POST /api/dev/chat/stream（M7 删除）
│   └── config.py                  # 新增 dashscope_api_key 字段
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_llm_smoke.py          # Step 1 产物：真实调用 qwen3.5-flash（标 @pytest.mark.live）
│   ├── test_graph_stream.py       # Step 2 产物：LangGraph astream_events 事件形状
│   └── test_sse_endpoint.py       # Step 4 产物：SSE 端点集成测试（mock LLM）
└── pyproject.toml                 # 新增 langchain / langgraph / dashscope（移除 langchain-qwq）

mobile/
├── app/
│   └── dev-chat.tsx               # Demo 页面（M7 删除）
├── lib/
│   └── sseClient.ts               # react-native-sse 封装
└── package.json                   # 新增 react-native-sse
```

---

## 执行步骤

### Step 1：依赖引入 + ChatDashScopeQwen 烟雾测试

> ⚠️ M3 首轮执行已终止，代码已 revert 到 M3 执行前状态（`langchain-qwq` 已弃用）。重执行时从空白状态按本节 checklist 落地，同时沿用首轮偏差 1.1-1.4 的工程结论（见 [M3 执行偏差记录](https://www.notion.so/M3-0b31497d177d44d088989220915e33c4?pvs=21)）。
> 
- [x]  `backend/pyproject.toml` 新增依赖：`langchain` / `langgraph` / `dashscope`（**不要加** `langchain-qwq`）；新增 `[project.optional-dependencies].dev`：`pytest` / `pytest-asyncio` / `httpx` / `ruff` / `basedpyright`（沿用偏差 1.1）；`[tool.pytest.ini_options]` 追加 `markers = ["live: needs real LLM API"]`；不要写 `[tool.mypy]`（沿用偏差 1.3）
- [x]  `backend/Dockerfile` 引入 `ARG INSTALL_DEV=false` + `ENV UV_SYSTEM_PYPI_MIRROR` + 条件安装分支；`docker-compose.yml` 中 `api.build.args.INSTALL_DEV="true"`（沿用偏差 1.2）
- [x]  重新构建镜像：`docker compose build api`
- [x]  `.env.example` 与 `.env` 新增 `LB_DASHSCOPE_API_KEY=sk-xxx`；**同时**新增 `DASHSCOPE_API_KEY=${LB_DASHSCOPE_API_KEY}`（DashScope SDK 默认读取后者；scratch 裸调脚本若未显式传 `api_key=` 会读不到 `LB_` 前缀变量）
- [x]  `app/config.py` 新增 `dashscope_api_key: SecretStr` 字段（沿用偏差 1.4：用 `SecretStr` 而非裸 `str`）
- [x]  🔴 **先裸调诊断**：写 scratch 脚本直调 `from dashscope.aigc.multimodal_conversation import AioMultiModalConversation; await AioMultiModalConversation.call(...)`，确认 API key + SDK 链路正常（把「SDK 链路」和「薄包装」的失败模式分开）。至少覆盖以下实测项并写入偏差 3.x：
    - `output.choices[0].message.content` 是 str 还是 list？`finish_reason` / `usage` 字段位置（仅末条还是每条 chunk 都有）
    - `result_format` / `incremental_output` / `enable_thinking` 的**参数位置**：作为顶层 kwarg 传入 vs 包进 `parameters={}` dict，两种写法各跑一次确认哪种被 SDK 识别（agno #4290 / pi-mono #2770 都踩过静默忽略）
    - **关闭思考验证**：遍历所有 chunk，断言 `message.reasoning_content` 始终为空字符串或字段不存在（只断言 `content` 非空会漏掉思考内容流回的假阳性）
    - `timeout` 的 kwarg 命名（`timeout` vs `request_timeout`）以及是否必须走 `parameters={}`；M3 流式场景 `max_retries=0` 是硬约束（重试会破坏流语义）
    - `response.model` 字段记录 `qwen3.5-flash` 别名解析到的具体快照版本号（如 `qwen3.5-flash-2026-02-23`），M6 上线时考虑固定快照
- [x]  新建 `app/chat/dashscope_chat.py`：`ChatDashScopeQwen(BaseChatModel)` 薄包装，M3 只实现 `_astream` + `_agenerate`（后者用 `agenerate_from_stream` 转接，不手写）+ `enable_thinking` 参数（`with_structured_output` 留 M8、`bind_tools` 留 M9、图像输入留 M10、内置工具留 M18+）
- [x]  薄包装内部必须把 `response.status_code != HTTPStatus.OK` 显式转为异常（防止错误消息被塞进 `AIMessage.content` 导致下游假阳性 PASS，M3 首轮教训）
- [x]  新建 `app/chat/llm.py`：构造并返回单例 `ChatDashScopeQwen`
- [x]  写 `tests/test_dashscope_chat.py`：覆盖 `_agenerate` / `_astream` / 消息格式转换（确认 content 包成 `list[{"text": ...}]`） / 错误路径（401 / 超时 / 空响应），mock `AioMultiModalConversation.call`
- [x]  写 `tests/test_llm_smoke.py`，断言强化：`isinstance(result, AIMessage)` + `result.usage_metadata.input_tokens > 0` + `finish_reason in {"stop", "length"}`（禁止仅断言 `len(content) > 0`，M3 首轮教训）

```python
# backend/app/chat/llm.py
from functools import lru_cache

from app.chat.dashscope_chat import ChatDashScopeQwen
from app.config import settings

@lru_cache(maxsize=1)
def get_chat_llm() -> ChatDashScopeQwen:
    """构造主对话 LLM 单例。

    为什么走 DashScope 原生端而不是百炼 OpenAI 兼容端：
    - 兼容端对内置工具（code_interpreter / web_search / MCP）支持不完整，长期需切 Responses API；
    - `langchain-qwq` 在 M3 Step 3 实测百炼兼容端 401；
    - DashScope 原生 SDK 由阿里官方维护，Qwen 新特性首发通道，可控性最高。

    为什么关闭思考模式：M3 做流式链路验证，思考阶段产生的 reasoning_content
    会延迟首个 content token 的到达，干扰"首 token 延迟"这一核心验证指标。
    M6 / M8 再根据场景决定是否启用。
    """
    return ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key=settings.dashscope_api_key,  # SecretStr，薄包装内部 .get_secret_value()
        enable_thinking=False,
        # 超时控制 —— 境内调用通常 <5s，给 30s 足够。
        timeout=30,
        # 流式场景重试无意义；上层做 error 事件透传。
        max_retries=0,
    )
```

```python
# backend/app/chat/dashscope_chat.py —— 薄包装骨架（M3 阶段，≈ 80 行）
# 注：qwen3.5-flash / qwen3.6-plus 均为原生多模态模型，必须走 AioMultiModalConversation；
# 走 AioGeneration 会报 400 url error。import 路径 Step 1 裸调确认。
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any

from dashscope.aigc.multimodal_conversation import AioMultiModalConversation
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import (
    BaseChatModel,
    agenerate_from_stream,
)
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from pydantic import SecretStr

class DashScopeAPIError(Exception):
    """DashScope 非 200 响应统一抖出来，给上层 error 事件接收。"""
    def __init__(self, code: str, message: str, request_id: str) -> None:
        super().__init__(f"[{code}] {message} (request_id={request_id})")
        self.code, self.message, self.request_id = code, message, request_id

class ChatDashScopeQwen(BaseChatModel):
    model: str
    api_key: SecretStr
    enable_thinking: bool = False
    timeout: int = 30
    max_retries: int = 0
    # 显式固定为 False，防止下游误改致 astream_events 退化为非流式
    disable_streaming: bool = False

    @property
    def _llm_type(self) -> str:
        return "dashscope-qwen"

    def _to_dashscope_messages(self, messages: list[BaseMessage]) -> list[dict]:
        # 多模态接口：content 必须是 list[dict]，纯文本也包 [{"text": "..."}]
        # Human/AI/System 映射 user/assistant/system
        role_map = {"human": "user", "ai": "assistant", "system": "system"}
        out: list[dict] = []
        for m in messages:
            text = m.content if isinstance(m.content, str) else str(m.content)
            out.append({"role": role_map[m.type], "content": [{"text": text}]})
        return out

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """流式调用 DashScope 多模态接口并逐 chunk yield ChatGenerationChunk。"""
        # timeout / max_retries 的接入方式待 Step 1 裸调确认（DashScope SDK 的 kwarg
        # 名可能是 timeout / request_timeout / 或需走 parameters={}），届时按偏差 3.x 回填。
        # result_format / incremental_output / enable_thinking 同理：Step 1 需实测验证
        # 究竟是顶层 kwarg 生效还是必须包进 parameters={}，避免参数被 SDK 静默忽略。
        responses = await AioMultiModalConversation.call(
            model=self.model,
            api_key=self.api_key.get_secret_value(),
            messages=self._to_dashscope_messages(messages),
            result_format="message",
            stream=True,
            incremental_output=True,  # 关键：令 chunk 为本次增量而非累计全文
            enable_thinking=self.enable_thinking,
        )
        async for response in responses:
            if response.status_code != HTTPStatus.OK:
                # 显式转异常；禁止让错误消息被塞进 AIMessage.content
                raise DashScopeAPIError(response.code, response.message, response.request_id)
            choice = response.output.choices[0]
            # 多模态接口的 message.content 常为 list[{"text": "..."}]或 str；
            # Step 1 裸调确认后如有偏差再调整。
            raw = choice.message.content
            if isinstance(raw, list):
                delta = "".join(part.get("text", "") for part in raw if isinstance(part, dict))
            else:
                delta = raw or ""
            if delta:
                yield ChatGenerationChunk(message=AIMessageChunk(content=delta))
            # 关键：LangChain AIMessageChunk.__add__ 会把 usage_metadata 的 token 数相加。
            # DashScope 流式有时会在每条 chunk 都附 usage（累积值），中途 yield 会导致
            # 最终 input_tokens 翻倍。只在 finish_reason 非空的末条 chunk 上透传 usage。
            if choice.finish_reason:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        response_metadata={"finish_reason": choice.finish_reason},
                        usage_metadata={
                            "input_tokens": response.usage.input_tokens,
                            "output_tokens": response.usage.output_tokens,
                            "total_tokens": response.usage.total_tokens,
                        } if response.usage else None,
                    )
                )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # 复用 _astream；避免两条路径分别维护
        return await agenerate_from_stream(self._astream(messages, stop, run_manager, **kwargs))
```

> `_astream` 骨架基于多模态接口推断写成（官方暂无 `AioMultiModalConversation` 的正式异步示例，依据 agentscope 社区修复实践推断）。import 路径 / `output.choices[0].message.content` 格式（str 还是 list） / `finish_reason` / `response.usage` 位置均在 Step 1 裸调诊断时按实测结果对齐，如有偏差记到偏差 3.x 。
> 

```python
# backend/tests/test_llm_smoke.py
import pytest
from langchain_core.messages import AIMessage

from app.chat.llm import get_chat_llm

@pytest.mark.live  # 标记为需要真实 API 的测试，默认 CI 不跑
@pytest.mark.asyncio
async def test_qwen_flash_ainvoke_returns_text() -> None:
    """验证 ChatDashScopeQwen 在 Python 3.14 下可正常调用 qwen3.5-flash。

    断言强化：仅断言 content 非空在 SDK 把错误消息塞进 content 时会假阳性（M3 首轮教训）。
    必须同时验证 usage_metadata 真实回传 token 计数 + finish_reason 正常。
    """
    llm = get_chat_llm()
    result = await llm.ainvoke("用一句话回答：今天心情不错用一个词描述是什么？")
    assert isinstance(result, AIMessage)
    assert isinstance(result.content, str) and len(result.content) > 0
    assert result.usage_metadata is not None
    assert result.usage_metadata["input_tokens"] > 0
    assert result.response_metadata.get("finish_reason") in {"stop", "length"}
```

`pyproject.toml` 里新增 `pytest` 标记（`backend/pyproject.toml` 的 `[tool.pytest.ini_options]` 追加 `markers = ["live: needs real LLM API"]`）。

**验证**：`pytest -m live tests/test_llm_smoke.py -v` 通过；终端能看到 qwen3.5-flash 真实回复。

**提交**：`feat(chat): add ChatDashScopeQwen wrapper with qwen3.5-flash smoke test`

---

### Step 2：LangGraph 单节点流式图

> ⚠️ M3 首轮本 Step 曾通过，但代码已 revert；需以 `ChatDashScopeQwen` 为基础从空白状态重建，验证 `on_chat_model_stream` 事件形状不变、`incremental_output=True` 下 chunk 为增量。
> 
- [ ]  新建 `app/chat/graph.py`：定义 `ChatState` + 单节点图，显式使用四参 `CompiledStateGraph[ChatState, None, ChatState, ChatState]` 注解（沿用偏差 2.1 结论，从 `langgraph.graph.state` 导入）
- [ ]  用 `.astream_events(version="v2")` 跑一遍，确认能拿到 `on_chat_model_stream` 事件，chunk.content 为本轮增量而非累计
- [ ]  写 `tests/test_graph_stream.py`（mock LLM，不走真网络）；测试内断言 `get_chat_llm().disable_streaming is False`，防止误改导致流式退化

```python
# backend/app/chat/graph.py
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.chat.llm import get_chat_llm

class ChatState(TypedDict):
    """M3 最小状态：只含消息列表。

    M6 会扩展为完整主对话图的 State（含 audit_state / session_id / child_profile 等）。
    """
    messages: Annotated[list[BaseMessage], add_messages]

async def call_main_llm(state: ChatState) -> dict[str, list[BaseMessage]]:
    """唯一节点：调用 qwen3.5-flash 生成回复。

    不在这里做 streaming 消费，返回完整 AIMessage 即可；
    流式由外层 .astream_events() 从 on_chat_model_stream 事件透传。
    """
    llm = get_chat_llm()
    response = await llm.ainvoke(state["messages"])
    return {"messages": [response]}

def build_chat_graph():
    """构造 M3 主对话图（单节点）。"""
    builder = StateGraph(ChatState)
    builder.add_node("call_main_llm", call_main_llm)
    builder.add_edge(START, "call_main_llm")
    builder.add_edge("call_main_llm", END)
    return builder.compile()
```

**验证**：

- 本地脚本 `python -m scripts.stream_probe`（临时 scratch，不入库）调用 `graph.astream_events(...)`，能看到 `on_chat_model_stream` 事件流；
- `test_graph_stream.py` 用 `FakeListChatModel` 替身验证图可正常编排。

**提交**：`feat(chat): add minimal LangGraph with streaming support`

---

### Step 3：FastAPI SSE 端点 + 事件协议

- [ ]  新建 `app/chat/sse.py`：SSE 事件序列化 + 生成器
- [ ]  新建 `app/api/dev_chat.py`：`POST /api/dev/chat/stream`
- [ ]  `app/main.py` 注册路由（include_router）
- [ ]  `app/main.py` 挂 `CORSMiddleware`，允许 Expo 真机 / 模拟器跨域请求（`allow_origins=["*"]` / `allow_methods=["POST", "OPTIONS"]` / `allow_headers=["*"]`）；M7 清理时收窄作用域或整体删除
- [ ]  手测：curl 发请求能看到逐行 SSE 输出

```python
# backend/app/chat/sse.py
import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import anyio
from langchain_core.messages import HumanMessage

from app.chat.graph import build_chat_graph

def _sse_pack(event_type: str, **payload: Any) -> str:
    """SSE 单条消息序列化。

    为什么不用 event: 字段而是 type 放 data 里：客户端统一 JSON 解析，少一层分支；
    后续要加新事件类型时不需要动前端 SSE 解析层。
    """
    body = json.dumps({"type": event_type, **payload}, ensure_ascii=False)
    return f"data: {body}\n\n"

async def stream_chat(user_message: str, session_id: str) -> AsyncIterator[str]:
    """将 LangGraph 流式事件转换为 SSE 帧。"""
    yield _sse_pack("start", session_id=session_id)

    graph = build_chat_graph()
    try:
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content=user_message)]},
            version="v2",
        ):
            # on_chat_model_stream 是 LangChain 定义的 token 级事件
            # data.chunk 是 AIMessageChunk，content 为本次增量文本
            if event["event"] == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                if chunk.content:
                    yield _sse_pack("delta", content=chunk.content)
    except (asyncio.CancelledError, anyio.ClientDisconnected):
        # 客户端断开：不发 error 事件（连接已关，写入会回爆堆栈），
        # 让 Starlette 按正常取消流程清理上游 httpx 连接。
        raise
    except Exception as exc:  # noqa: BLE001 —— SSE 错误透传需要兜住所有上游真实异常
        yield _sse_pack("error", message=str(exc), code=type(exc).__name__)
        return

    yield _sse_pack("end", finish_reason="stop")
```

```python
# backend/app/api/dev_chat.py
"""M3 开发调试路由。M7 聊天界面正式版上线后整文件删除。"""
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.chat.sse import stream_chat

router = APIRouter(prefix="/api/dev", tags=["dev"])

class DevChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)

@router.post("/chat/stream")
async def dev_chat_stream(payload: DevChatRequest, request: Request) -> StreamingResponse:
    """M3 Demo：单轮流式对话，不鉴权、不落库。

    客户端断开由 Starlette 自动传播 CancelledError 到 stream_chat 生成器，
    由 httpx 客户端（`dashscope` SDK 底层）释放上游连接 —— 无需手动 is_disconnected 轮询。
    """
    session_id = str(uuid.uuid4())
    return StreamingResponse(
        stream_chat(payload.message, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 防止 nginx / 反代缓冲
            "Connection": "keep-alive",
        },
    )
```

**验证**：

```bash
curl -N -X POST http://localhost:8000/api/dev/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message":"讲一个 50 字的小故事"}'
```

能逐行看到 `data: {"type":"start",...}` → 多条 `delta` → `end`。手动 `Ctrl+C` 断开后，后端日志无异常堆栈。

**提交**：`feat(api): add SSE streaming endpoint for dev chat`

---

### Step 4：SSE 端点集成测试

- [ ]  用 `httpx.AsyncClient` + FakeListChatModel 替身写集成测试
- [ ]  覆盖三条路径：正常流 / 上游异常 / 客户端提前断开

```python
# backend/tests/test_sse_endpoint.py
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

def _parse_sse_lines(body: str) -> list[dict]:
    """把 SSE 响应体拆成事件列表。"""
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events

@pytest.mark.asyncio
async def test_sse_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常流：至少包含 start / delta / end 三类事件。"""
    # 注意：必须用 GenericFakeChatModel（支持流式），不是 FakeListChatModel（不支持）
    # 后者不会触发 on_chat_model_stream，delta 事件会缺失
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.chat.llm import get_chat_llm

    # 坑点 1：`get_chat_llm` 在 graph.py 里已 `from app.chat.llm import get_chat_llm` 绑定。
    #         必须 patch **使用点** `app.chat.graph.get_chat_llm`，而不是定义点
    #         `app.chat.llm.get_chat_llm`，否则 graph 内部仍拿到原函数（Python import 绑定陷阱）。
    # 坑点 2：`get_chat_llm` 被 `@lru_cache` 装饰，跨测试会污染 —— 先清缓存再 patch。
    get_chat_llm.cache_clear()
    monkeypatch.setattr(
        "app.chat.graph.get_chat_llm",
        lambda: GenericFakeChatModel(messages=iter([AIMessage(content="你好，小盒子")])),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/dev/chat/stream", json={"message": "hi"})
        assert resp.status_code == 200
        events = _parse_sse_lines(resp.text)

    assert events[0]["type"] == "start"
    assert any(e["type"] == "delta" for e in events)
    assert events[-1]["type"] == "end"
```

> 异常路径与断开路径也各写一个用例（monkeypatch 让 LLM 抛异常；用 `client.stream()` + 提前 `break` 模拟客户端断开）。限于篇幅不全贴，参考 M2 测试风格自行展开。
> 

**验证**：`pytest tests/test_sse_endpoint.py -v` 全绿。

**提交**：`test(chat): add SSE endpoint integration tests`

---

### Step 5：RN SSE 客户端封装

- [ ]  `cd mobile && npx expo install react-native-sse`
- [ ]  新建 `mobile/lib/sseClient.ts`：类型安全的事件订阅封装

```tsx
// mobile/lib/sseClient.ts
import EventSource from 'react-native-sse'

export type ChatSseEvent =
	| { type: 'start'; session_id: string }
	| { type: 'delta'; content: string }
	| { type: 'end'; finish_reason: string }
	| { type: 'error'; message: string; code: string }

export interface ChatSseHandle {
	close: () => void
}

export function openChatStream(
	baseUrl: string,
	message: string,
	handlers: {
		onEvent: (e: ChatSseEvent) => void
		onTransportError?: (err: unknown) => void
	},
): ChatSseHandle {
	// react-native-sse 支持 POST + body，原生 EventSource 不支持，这是选它的关键原因。
	const es = new EventSource(`${baseUrl}/api/dev/chat/stream`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ message }),
	})

	es.addEventListener('message', (ev) => {
		if (!ev.data) return
		try {
			const parsed = JSON.parse(ev.data) as ChatSseEvent
			handlers.onEvent(parsed)
			if (parsed.type === 'end' || parsed.type === 'error') es.close()
		} catch (err) {
			handlers.onTransportError?.(err)
		}
	})
	es.addEventListener('error', (err) => {
		// react-native-sse 在 close() 后仍会触发一次 error（CONNECTING → CLOSED 转换），
		// 过滤掉已关闭状态的误报，避免 UI 误跳 error 态。
		if (es.readyState === 2 /* CLOSED */) return
		handlers.onTransportError?.(err)
	})

	return { close: () => es.close() }
}
```

**验证**：TypeScript 编译通过；在 dev-chat 页面被成功 import。

**提交**：`feat(mobile): add SSE client wrapper for chat streaming`

---

### Step 6：Demo 页面 + 流式渲染 + 中断按钮

- [ ]  新建 `mobile/app/dev-chat.tsx`
- [ ]  界面：输入框 + 发送按钮 + 停止按钮 + AI 回复区（逐字追加）
- [ ]  状态机：`idle / streaming / done / error`

```tsx
// mobile/app/dev-chat.tsx
import { useRef, useState } from 'react'
import { Button, Platform, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native'

import { ChatSseHandle, openChatStream } from '../lib/sseClient'

type Status = 'idle' | 'streaming' | 'done' | 'error'

// 开发地址：Android 模拟器用 10.0.2.2，iOS 模拟器 / web 用 localhost，真机用电脑局域网 IP。
// 生产走 EXPO_PUBLIC_API_BASE 环境变量；M7 清理时此常量一并删除。
const API_BASE =
	process.env.EXPO_PUBLIC_API_BASE ??
	(Platform.OS === 'android' ? 'http://10.0.2.2:8000' : 'http://localhost:8000')

export default function DevChat() {
	const [input, setInput] = useState('')
	const [reply, setReply] = useState('')
	const [status, setStatus] = useState<Status>('idle')
	const [errMsg, setErrMsg] = useState<string | null>(null)
	const handleRef = useRef<ChatSseHandle | null>(null)

	const send = () => {
		if (!input.trim() || status === 'streaming') return
		setReply('')
		setErrMsg(null)
		setStatus('streaming')

		handleRef.current = openChatStream(API_BASE, input, {
			onEvent: (e) => {
				switch (e.type) {
					case 'delta':
						setReply((prev) => prev + e.content)
						break
					case 'end':
						setStatus('done')
						break
					case 'error':
						setErrMsg(e.message)
						setStatus('error')
						break
				}
			},
			onTransportError: (err) => {
				setErrMsg(String(err))
				setStatus('error')
			},
		})
	}

	const stop = () => {
		handleRef.current?.close()
		setStatus('done')
	}

	return (
		<View style={styles.container}>
			<Text style={styles.title}>M3 Streaming Demo（M7 后删除）</Text>
			<TextInput
				style={styles.input}
				value={input}
				onChangeText={setInput}
				placeholder="输入一条消息"
				editable={status !== 'streaming'}
			/>
			<View style={styles.row}>
				<Button title="发送" onPress={send} disabled={status === 'streaming'} />
				<Button title="停止" onPress={stop} disabled={status !== 'streaming'} />
			</View>
			<Text style={styles.status}>状态：{status}</Text>
			<ScrollView style={styles.replyBox}>
				<Text>{reply}</Text>
				{errMsg && <Text style={styles.err}>错误：{errMsg}</Text>}
			</ScrollView>
		</View>
	)
}

const styles = StyleSheet.create({
	container: { flex: 1, padding: 16, gap: 12 },
	title: { fontSize: 16, fontWeight: '600' },
	input: { borderWidth: 1, borderColor: '#ccc', borderRadius: 8, padding: 8 },
	row: { flexDirection: 'row', gap: 12 },
	status: { color: '#666' },
	replyBox: { flex: 1, borderWidth: 1, borderColor: '#eee', borderRadius: 8, padding: 8 },
	err: { color: 'red', marginTop: 8 },
})
```

> iOS 真机 / Android 真机请把 `API_BASE` 改为电脑局域网 IP（`http://192.168.x.x:8000`）。Android 模拟器用 `10.0.2.2`，iOS 模拟器可用 `localhost`。
> 

**验证**：

- Expo Go 打开 `/dev-chat` 路由
- 输入"讲一个 50 字的小故事" → 发送 → 回复逐字出现 → 末尾状态变 `done`
- 流式过程中按"停止" → 回复停止增长，状态变 `done`，后端日志显示客户端断开、无异常
- 拔网 / 关掉后端 → 状态变 `error`，有可读错误信息

**提交**：`feat(mobile): add dev-chat demo page with streaming + stop`

---

### Step 7：端到端验证与结论记录

- [ ]  按下方"验收矩阵"逐项打勾
- [ ]  在本页末尾补一段"**验证结论**"：通过 / 需调整 + 原因
- [ ]  若通过，M6 直接沿用本技术栈；若需调整，在 `M3 执行偏差记录` 子页详述

**提交**：`docs: record M3 streaming validation result`

---

## 验收矩阵

| 验收项 | 通过标准 | 结果 |
| --- | --- | --- |
| 中断收尾 | 客户端 stop / 断开后，后端无异常堆栈，httpx 连接释放 | [ ] |
| Python 3.14 + `dashscope` SDK 兼容性 | Step 1 smoke test 通过（强化断言） | ✅ |
| 思考模式确实关闭 | Step 1 裸调 → `enable_thinking=False` 下 `reasoning_tokens=0`（偏差 1.7） | ✅ |
| usage_metadata 不被累加翻倍 | 薄包装 `_astream` 仅在末条 chunk yield usage（偏差 1.8 白名单守卫）；mock + live smoke 均验证 | ✅ |
| SSE 集成测试 | Step 4 happy / error / disconnect 三路径全绿 | [ ] |
| 首 content token 延迟 < 2s | 三类 prompt 中位数均 < 1s（偏差 3.3：0.40 / 0.41 / 0.43s） | ✅ |

---

## M7 清理清单（预埋，到时候直接对账）

- [ ]  删除 `backend/app/api/dev_chat.py`
- [ ]  从 `app/main.py` 移除 `dev_chat.router` 注册
- [ ]  删除 `mobile/app/dev-chat.tsx`
- [ ]  保留并迁移：`app/chat/llm.py`、`app/chat/graph.py`（扩展为完整主对话图）、`app/chat/sse.py`（协议已定型，M6/M9 复用）、`mobile/lib/sseClient.ts`

---

## ⚠️ 待确认

目前无。所有决策点已在对齐阶段敲定。实施过程中若出现新未决点，在"M3 执行偏差记录"子页累积。

---

**最终提交**：合并到 main 分支，M3 完成 ✅

[M3 执行偏差记录](https://www.notion.so/M3-0b31497d177d44d088989220915e33c4?pvs=21)