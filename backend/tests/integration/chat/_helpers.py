"""阶段二红测共享助手：FakeLLM + 种子数据 + SSE 解析。

子代理核实结论（闸门 A 预验证）：
  - 主图 LLM：provider key="deepseek"，调用方法 .astream() → AIMessageChunk
  - 审查图 LLM：provider key="audit_deepseek"，调用方法 .ainvoke() → AIMessage
  - 审查图结构化输出：通过 bind_tools([AuditOutputSchema]) 的 tool_call 机制
  - register_chat_task：注册 asyncio.Task，await 返回 None，异常会传播
  - running_streams：dict[str, asyncio.Event]，模块级在 locks.py
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, UsageMetadata


class FakeMainLLM:
    """可编排的主图 FakeLLM：astream 输出可控 delta / finish_reason / usage。

    生命周期测试（Steps 16–20）通过本 fake 编排不同的流式行为：
      - chunks：控制 delta 帧内容和数量
      - finish_reason：控制末帧 finish_reason（stop / length / content_filter）
      - usage_metadata：控制 commit② 中 usage 记账和 compression 触发
      - inject_error：在指定 chunk 后抛出异常（测试段一错误处理）
    """

    def __init__(
        self,
        chunks: list[str] | None = None,
        finish_reason: str = "stop",
        usage_metadata: dict[str, int] | None = None,
        delay: float = 0,
    ) -> None:
        self._chunks = chunks or ["你好，", "我是", "AI助手。"]
        self._finish_reason = finish_reason
        self._usage_metadata = usage_metadata or {
            "input_tokens": 15,
            "output_tokens": 25,
            "total_tokens": 40,
        }
        self._delay = delay

    async def astream(
        self,
        messages: Any,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        """产生可控的 AIMessageChunk 流。

        每条 chunk.content 对应一个 delta 帧。
        末 chunk 携带 finish_reason 和 usage_metadata。
        delay > 0 时每条 chunk 间 sleep（用于 stop 并发窗口测试）。
        """
        import asyncio

        last_idx = len(self._chunks) - 1
        for i, text in enumerate(self._chunks):
            if self._delay and i > 0:
                await asyncio.sleep(self._delay)
            kw: dict[str, Any] = {"content": text}
            if i == last_idx:
                kw["response_metadata"] = {"finish_reason": self._finish_reason}
                kw["usage_metadata"] = UsageMetadata(**self._usage_metadata)
            yield AIMessageChunk(**kw)

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeMainLLM":
        return self

    def with_retry(self, **kwargs: Any) -> "FakeMainLLM":
        return self

    def with_fallbacks(
        self,
        fallbacks: Any,
        **kwargs: Any,
    ) -> "FakeMainLLM":
        return self


class FakeAuditLLM:
    """可编排的审查图 FakeLLM：ainvoke 输出可控 AIMessage。

    子代理核实结论：
      审查图不使用 with_structured_output，而是通过 bind_tools 注册
      AuditOutputSchema 为 tool，LLM 通过 tool_call 返回结构化数据。
      audit_llm_call 节点先 ainvoke，无 tool_calls 时追加提示重试，
      仍无则降级默认值。

    对于阶段二红测（入队名 bug 导致审查 worker 从不执行），
    本 fake 只须不崩溃即可（audit LLM 在 RED 阶段不会被调用到）。
    但为阶段三修复做准备，本 fake 支持通过 _tool_calls 控制输出。
    """

    def __init__(
        self,
        content: str = "审查无异常",
        tool_calls: list[dict] | None = None,
    ) -> None:
        self._content = content
        self._tool_calls = tool_calls

    async def ainvoke(
        self,
        messages: Any,
        **kwargs: Any,
    ) -> AIMessage:
        if self._tool_calls:
            return AIMessage(content="", tool_calls=self._tool_calls)
        return AIMessage(content=self._content)

    async def astream(
        self,
        messages: Any,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        """供危机/红线干预 LLM（call_crisis_llm / call_redline_llm）调用。
        它们使用与审查图相同的 provider key "audit_deepseek"，
        但调的是 .astream() 而非 .ainvoke()。
        """
        yield AIMessageChunk(content=self._content)

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeAuditLLM":
        return self

    def with_retry(self, **kwargs: Any) -> "FakeAuditLLM":
        return self

    def with_fallbacks(
        self,
        fallbacks: Any,
        **kwargs: Any,
    ) -> "FakeAuditLLM":
        return self


def make_audit_tool_call(
    crisis_detected: bool = False,
    crisis_topic: str | None = None,
    redline_triggered: bool = False,
    redline_detail: str | None = None,
    guidance_injection: str | None = None,
    turn_summary: str = "审查正常",
) -> list[dict]:
    """构造 FakeAuditLLM 可用的 tool_calls 参数，模拟 AuditOutputSchema 工具调用。

    audit_llm_call 节点期望 AIMessage.tool_calls[0].name == "AuditOutputSchema"，
    且 args 可通过 AuditOutputSchema.model_validate() 校验。
    AuditOutputSchema 的 model_validator 要求：
      - crisis_detected=True → crisis_topic 非空
      - redline_triggered=True → redline_detail 非空

    注意：args 键 "guidance_injection" 必须与 AuditOutputSchema 当前字段名一致；
    pydantic v2 extra=ignore 会静默丢弃键名不匹配的字段，导致集成测试断言失败。
    """
    import uuid

    args = {
        "dimension_scores": {
            "emotional": 0, "social": 0, "romance": 0, "values": 0,
            "boundaries": 0, "academic": 0, "lifestyle": 0,
        },
        "crisis_detected": crisis_detected,
        "crisis_topic": crisis_topic,
        "redline_triggered": redline_triggered,
        "redline_detail": redline_detail,
        "guidance_injection": guidance_injection,
        "turn_summary": turn_summary,
    }
    return [{
        "name": "AuditOutputSchema",
        "args": args,
        "id": f"call-{uuid.uuid4().hex[:12]}",
    }]


async def seed_integration_child(integration_runtime: Any) -> tuple[Any, dict[str, str]]:
    """在集成库中创建 child user + family + auth token。

    返回 (child_user, auth_headers)。
    auth_headers 含 Authorization + X-Device-Id，可直接用于 api_client 请求。

    与本 Step 关注点的关系：
      - 使用 integration_runtime.db_session_factory() 连集成库
      - 使用 integration_runtime.audit_redis 写 token 缓存
      - auth 链路的 get_db / get_redis 被 conftest 覆写指向集成实例，
        commit_with_redis 显式传入的 redis 与 auth dep 使用的 redis 同源
    """
    import uuid

    from app.core.enums import UserRole
    from app.core.redis import commit_with_redis
    from app.domain.accounts.models import Family, FamilyMember, User
    from app.domain.auth.tokens import issue_token

    device_id = f"test-dev-{uuid.uuid4().hex[:8]}"

    async with integration_runtime.db_session_factory() as sess:
        fam = Family()
        sess.add(fam)
        await sess.flush()

        user = User(
            family_id=fam.id,
            role=UserRole.child,
            phone="0000",
            is_active=True,
        )
        sess.add(user)
        await sess.flush()

        sess.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))

        token = await issue_token(
            sess,
            user_id=user.id,
            role=UserRole.child,
            family_id=user.family_id,
            device_id=device_id,
            ttl_days=None,  # child token 永不过期
        )
        await commit_with_redis(sess, integration_runtime.audit_redis)

    return user, {
        "Authorization": f"Bearer {token}",
        "X-Device-Id": device_id,
    }


async def parse_sse_events(response: Any) -> list[tuple[str, dict[str, Any]]]:
    """解析 SSE 多行协议帧。

    帧格式：event: <type>\\ndata: <json>\\n\\n
    返回 [(event_type, data_dict), ...] 列表。
    """
    events: list[tuple[str, dict[str, Any]]] = []
    current_event: str | None = None
    data_parts: list[str] = []

    async for line in response.aiter_lines():
        if line.startswith("event: "):
            current_event = line[7:]
            data_parts = []
        elif line.startswith("data: "):
            data_parts.append(line[6:])
        elif line == "" and current_event is not None:
            events.append((current_event, json.loads("".join(data_parts))))
            current_event = None
            data_parts = []

    return events
