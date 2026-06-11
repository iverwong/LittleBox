"""审查 LLM 装配测试（Step 6 新增）。

落点:验证 `app/domain/audit/llm.py::build_audit_llm` 重写后,主备两端都
正确绑了 3 个工具([AppendNote, ReplaceInNotes, AuditOutputSchema])。

设计:
- 用 respx 在 HTTP 层 mock,捕获两 URL 的 request body
- 主端持续 ConnectError 触发 retry×3 + fallback 兜底,保证双 URL 各被访问
- 断言每条 request body 的 `tools` 数组含 3 个 function schema 且名称匹配
- thinking=enabled 覆盖由 Step 1/2 拓扑单测通过 `Role.AUDIT.thinking=True`
  路径(走 `_adapter_chat_deepseek`)实证,本测试不重复抓 body.thinking
  (若 respx 抓不到该字段会破坏本测试可读性)

Step 7 不涉及本文件:test_factory.py 整体重写目标,本测试落在
tests/audit/ 域,build_audit_llm 属 app/domain/audit,域归属一致,
避开 Step 7 churn。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from app.core.config import settings
from app.core.llm_topology import ENDPOINTS, EndpointName
from app.domain.audit.llm import build_audit_llm
from langchain_core.messages import HumanMessage

_EXPECTED_TOOL_NAMES = frozenset({"AppendNote", "ReplaceInNotes", "AuditOutputSchema"})


def _success_response() -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }


def _tool_names_from_request_body(body: bytes) -> frozenset[str]:
    """从 ChatDeepSeek POST body 中抽出工具 function name 集合。"""
    payload = json.loads(body)
    tools = payload.get("tools") or []
    return frozenset(t["function"]["name"] for t in tools)


class TestAuditLlmToolBinding:
    """build_audit_llm 装配链:主备两端各 bind 3 工具,双 URL 均被请求。"""

    @pytest.fixture
    def _no_retry_backoff(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        """把 tenacity 退避入口 `asyncio.sleep` 置 no-op,使重试真实发生但耗时归零。

        wrap_resilience 内部 `with_retry(wait_exponential_jitter=True)` 默认
        走 asyncio.sleep 做指数退避,若不拦截,本测试在 CI 上拖慢数十毫秒。
        """
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)
        return sleep_mock

    async def test_both_urls_receive_three_tool_schemas(
        self, _no_retry_backoff: AsyncMock
    ) -> None:
        """主端持续失败 → retry×3 → fallback 兜底:两 URL 各收到带 3 工具的请求。"""
        primary_url = f"{ENDPOINTS[EndpointName.DEEPSEEK].base_url}/chat/completions"
        fallback_url = f"{ENDPOINTS[EndpointName.BAILIAN].base_url}/chat/completions"

        primary_bodies: list[bytes] = []
        fallback_bodies: list[bytes] = []

        def capture_primary(request: httpx.Request) -> httpx.Response:
            primary_bodies.append(request.content)
            raise httpx.ConnectError("mock connection refused")

        def capture_fallback(request: httpx.Request) -> httpx.Response:
            fallback_bodies.append(request.content)
            return httpx.Response(200, json=_success_response())

        async with respx.mock(assert_all_mocked=False) as respx_mock:
            respx_mock.post(primary_url).mock(side_effect=capture_primary)
            respx_mock.post(fallback_url).mock(side_effect=capture_fallback)

            llm = build_audit_llm(settings)
            result = await llm.ainvoke([HumanMessage(content="你好")])
            assert result.content is not None

        # 主端被 retry 3 次(ROLES[AUDIT].retry_attempts=3)
        assert len(primary_bodies) == 3
        # 备端被调 1 次(fallback 一次性成功)
        assert len(fallback_bodies) == 1

        # 主备 4 条请求 body 全部含 3 工具 schema,名称集合一致
        for body in primary_bodies + fallback_bodies:
            names = _tool_names_from_request_body(body)
            assert names == _EXPECTED_TOOL_NAMES, (
                f"期望工具集 {_EXPECTED_TOOL_NAMES},实际 {names}"
            )
