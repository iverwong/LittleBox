"""审查图节点 Runtime DI 直接断言（T16 H5）。

去重边界（H5）：不通过整图 ainvoke，直接节点函数级测试：
- load_context：验证 _load_messages_from_pg 被调用时第二个参数 == runtime.context.db_session_factory
- audit_llm_call：验证 build_audit_llm 被调用时参数 == runtime.context.settings
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.domain.audit.graph import AuditGraphState, audit_llm_call, audit_tools, load_context
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

pytestmark = [pytest.mark.audit, pytest.mark.asyncio]

SID = "00000000-0000-0000-0000-000000000001"
CUID = "00000000-0000-0000-0000-000000000002"


def _make_state(**overrides: object) -> AuditGraphState:
    state: AuditGraphState = {
        "sid": SID,
        "turn_number": 1,
        "child_profile": None,
        "session_notes_working": "",
        "tool_iter_count": 0,
        "structured_output": None,
        "messages": [],
        "max_iter": 5,
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


def _make_fake_runtime() -> object:
    """构造最小 Runtime[AuditContextSchema] 替代（SimpleNamespace 范式）。"""
    from types import SimpleNamespace

    from app.domain.audit.context_schema import AuditContextSchema

    ctx = AuditContextSchema(
        session_id=SID,
        child_user_id=CUID,
        target_message_id=SID,  # 测试用任意 UUID
        max_iter=5,
        settings=MagicMock(),
        db_session_factory=MagicMock(),
        audit_redis=MagicMock(),
    )
    return SimpleNamespace(context=ctx)


# ---------------------------------------------------------------------------
# 测试工具常量（移到顶部供所有新测试 class 共享）
# ---------------------------------------------------------------------------


def _tc(name: str, args: dict, call_id: str | None = None) -> dict:
    return {"name": name, "args": args, "id": call_id or f"call-{name}"}


_EMPTY_SCORES = {
    "emotional": 0,
    "social": 0,
    "romance": 0,
    "values": 0,
    "boundaries": 0,
    "academic": 0,
    "lifestyle": 0,
}

_AUDIT_OUTPUT_ARGS = {
    "dimension_scores": _EMPTY_SCORES,
    "crisis_detected": False,
    "crisis_topic": None,
    "redline_triggered": False,
    "redline_detail": None,
    "guidance_injection": "观察情绪走向",
    "turn_summary": "情绪稳定",
}

TOOL_NAME_APPEND = "AppendNote"
TOOL_NAME_REPLACE = "ReplaceInNotes"
TOOL_NAME_OUTPUT = "AuditOutputSchema"

_TC_REPLACE_MISS = _tc(TOOL_NAME_REPLACE, {"old_str": "不存在的文本", "new_str": "替换"})


async def test_load_context_passes_db_session_factory():
    """load_context 调 _load_messages_from_pg 和 _load_session_notes_from_pg 时均传 ctx.db_session_factory。"""
    state = _make_state()
    runtime = _make_fake_runtime()

    with (
        patch("app.domain.audit.graph._load_messages_from_pg", return_value=[]) as mock_load,
        patch(
            "app.domain.audit.graph._load_session_notes_from_pg",
            new=AsyncMock(return_value=""),
        ) as mock_load_notes,
    ):
        result = await load_context(state, runtime)

    # _load_messages_from_pg 被调一次，第二个参数 == runtime.context.db_session_factory
    mock_load.assert_awaited_once()
    args, _ = mock_load.await_args
    assert len(args) >= 2
    assert args[1] is runtime.context.db_session_factory, (
        "第二个参数应为 runtime.context.db_session_factory"
    )
    # _load_session_notes_from_pg 被调一次，参数 == (str(ctx.session_id), ctx.db_session_factory)
    mock_load_notes.assert_awaited_once()
    notes_args, _ = mock_load_notes.await_args
    assert notes_args[0] == str(runtime.context.session_id)
    assert notes_args[1] is runtime.context.db_session_factory
    # 返回值含 max_iter
    assert result.get("max_iter") == 5


async def test_load_context_returns_messages_with_max_iter():
    """load_context 返回 dict 含 messages、max_iter、session_notes_working。"""
    state = _make_state()
    runtime = _make_fake_runtime()

    with (
        patch("app.domain.audit.graph._load_messages_from_pg", return_value=[]),
        patch(
            "app.domain.audit.graph._load_session_notes_from_pg",
            new=AsyncMock(return_value=""),
        ),
    ):
        result = await load_context(state, runtime)

    assert "messages" in result
    assert "max_iter" in result
    assert result["max_iter"] == 5
    # A2 段:load_context 覆盖 session_notes_working,空 helper 返 ""
    assert result.get("session_notes_working") == ""
    # B 段:history 走 XML 包装后 messages 长度恒为 2 (System + Human)
    assert len(result["messages"]) == 2


async def test_audit_llm_call_passes_settings():
    """audit_llm_call 调 build_audit_llm 时传入 ctx.settings。

    验证 Runtime DI 正确注入 settings 参数（M8 期 closure 注入替代）。
    C2 段：纯文本追问路径仍走通。
    """
    state = _make_state()
    runtime = _make_fake_runtime()
    from langchain_core.messages import AIMessage

    with patch("app.domain.audit.graph.build_audit_llm") as mock_build:
        # 首次 ainvoke 返回纯文本（触发 post-processing 追问）
        first_ai = AIMessage(content="需要分析一下", tool_calls=[])
        # 第二次 ainvoke 返回 audit_output
        second_ai = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "AuditOutputSchema",
                    "args": {
                        "dimension_scores": {
                            "emotional": 0,
                            "social": 0,
                            "romance": 0,
                            "values": 0,
                            "boundaries": 0,
                            "academic": 0,
                            "lifestyle": 0,
                        },
                        "crisis_detected": False,
                        "crisis_topic": None,
                        "redline_triggered": False,
                        "redline_detail": None,
                        "guidance_injection": "ok",
                        "turn_summary": "ok",
                    },
                    "id": "call-2",
                }
            ],
        )

        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[first_ai, second_ai])
        mock_build.return_value = mock_llm

        result = await audit_llm_call(state, runtime)

    # build_audit_llm 被调一次，参数 == runtime.context.settings
    mock_build.assert_called_once_with(runtime.context.settings)
    # C2 段:纯文本追问后单 OUTPUT 透传,structured_output 不设(由 audit_tools 终止)
    assert "messages" in result
    assert result.get("structured_output") is None
    msgs = result["messages"]
    assert len(msgs) >= 1
    last_msg = msgs[-1]
    assert hasattr(last_msg, "tool_calls") and len(last_msg.tool_calls) > 0, (
        "末条消息应为含 tool_calls 的 AIMessage"
    )
    assert last_msg.tool_calls[0]["name"] == "AuditOutputSchema"


# ---------------------------------------------------------------------------
# 新增测试（计划 Step 4）:
# - TestLoadSessionNotes         (3 个 DB 测试)
# - TestLoadContextSeedsWorkingCopy (1)
# - TestLoadContextHistorySplit  (1)
# - TestRouteAfterLlmShortCircuit (2)
# - TestAuditToolsOutputViolation (2)
# - TestAuditToolsLastNotePayload (2)
# - TestAuditLlmCallParsesSingleOutput (3)
# - TestMaxIterTailFallback      (3)
# - TestRouteAfterToolsShortCircuit (1)
# ---------------------------------------------------------------------------


class TestLoadSessionNotes:
    """_load_session_notes_from_pg 三个分支（无行 / 有 notes / notes=NULL）。"""

    async def test_no_row_returns_empty(self, db_session, child_user):
        """session 无 RollingSummary 行 → 返 ""。"""
        import uuid
        from app.domain.audit.graph import _load_session_notes_from_pg
        from app.domain.chat.models import Session

        sid = uuid.uuid4()
        db_session.add(Session(id=sid, child_user_id=child_user.id, title="test"))
        await db_session.flush()

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _factory():
            yield db_session

        result = await _load_session_notes_from_pg(str(sid), _factory)  # type: ignore[arg-type]
        assert result == ""

    async def test_row_with_notes_returns_string(self, db_session, child_user):
        """种 RollingSummary 行 + 非空 session_notes → 返该字符串。"""
        import uuid
        from app.domain.audit.graph import _load_session_notes_from_pg
        from app.domain.audit.models import RollingSummary
        from app.domain.chat.models import Session

        # 先建 session(FK 约束)
        sid = uuid.uuid4()
        db_session.add(Session(id=sid, child_user_id=child_user.id, title="test"))
        await db_session.flush()

        rs = RollingSummary(
            session_id=sid,
            last_turn=0,
            session_notes="用户近期情绪稳定。",
        )
        db_session.add(rs)
        await db_session.flush()

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _factory():
            yield db_session

        result = await _load_session_notes_from_pg(str(sid), _factory)  # type: ignore[arg-type]
        assert result == "用户近期情绪稳定。"

    async def test_row_with_null_notes_returns_empty(self, db_session, child_user):
        """种 RollingSummary 行 + session_notes=None → 返 ""(防御 None)。"""
        import uuid
        from app.domain.audit.graph import _load_session_notes_from_pg
        from app.domain.audit.models import RollingSummary
        from app.domain.chat.models import Session

        sid = uuid.uuid4()
        db_session.add(Session(id=sid, child_user_id=child_user.id, title="test"))
        await db_session.flush()

        rs = RollingSummary(
            session_id=sid,
            last_turn=0,
            session_notes=None,
        )
        db_session.add(rs)
        await db_session.flush()

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _factory():
            yield db_session

        result = await _load_session_notes_from_pg(str(sid), _factory)  # type: ignore[arg-type]
        assert result == ""


class TestLoadContextSeedsWorkingCopy:
    """A2 段:load_context 把历史 session_notes 注入 + seed working copy。"""

    async def test_load_context_seeds_session_notes_working(self):
        state = _make_state()
        runtime = _make_fake_runtime()
        from langchain_core.messages import HumanMessage

        with (
            patch("app.domain.audit.graph._load_messages_from_pg", return_value=[]),
            patch(
                "app.domain.audit.graph._load_session_notes_from_pg",
                new=AsyncMock(return_value="历史笔记"),
            ),
        ):
            result = await load_context(state, runtime)

        # seed working copy
        assert result["session_notes_working"] == "历史笔记"
        # messages 长度恒为 2
        assert len(result["messages"]) == 2
        # 末条 HumanMessage.content 含 "历史笔记"
        last = result["messages"][-1]
        assert isinstance(last, HumanMessage)
        assert "历史笔记" in last.content


class TestLoadContextHistorySplit:
    """B 段:history 3+1 切分 + XML 包装,idx 重置从 1 起。"""

    async def test_history_split_3_plus_1(self):
        state = _make_state()
        runtime = _make_fake_runtime()
        from langchain_core.messages import AIMessage, HumanMessage

        # 模拟 4 轮 H/A 序列(8 条消息)
        history = [
            HumanMessage(content="h3"),
            AIMessage(content="a3"),
            HumanMessage(content="h2"),
            AIMessage(content="a2"),
            HumanMessage(content="h1"),
            AIMessage(content="a1"),
            HumanMessage(content="h0"),
            AIMessage(content="a0"),
        ]

        with (
            patch("app.domain.audit.graph._load_messages_from_pg", return_value=history),
            patch(
                "app.domain.audit.graph._load_session_notes_from_pg",
                new=AsyncMock(return_value="notes"),
            ),
        ):
            result = await load_context(state, runtime)

        last = result["messages"][-1]
        content = last.content
        # prior 段(3 轮 = 6 条)idx 1/2/3,每 idx 各 2 次(H+A)
        assert content.count('<turn idx="1"') == 4  # prior H+A + current H+A
        assert content.count('<turn idx="2"') == 2  # prior 段 H+A
        assert content.count('<turn idx="3"') == 2  # prior 段 H+A
        # <history> 根标签字面量出现 2 次 (prior + current 各一)
        assert content.count("<history>") == 2
        # session_notes 在末段
        assert "notes" in content


class TestAuditToolsSingleOutput:
    """单 OUTPUT 终止路径:audit_tools 校验 args, 校验通过则设 structured_output 终止,
    校验失败则发 error ToolMessage(带 validation_errors)触发下一轮修正。
    """

    async def test_single_output_valid_sets_structured_output(self):
        """单 OUTPUT 校验通过 → structured_output 设,M9.5 清掉 guidance_injection,
        不发 ToolMessage。"""
        from app.domain.audit.graph import audit_tools
        from langchain_core.messages import ToolMessage

        state = _make_state(
            session_notes_working="",
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[_tc(TOOL_NAME_OUTPUT, _AUDIT_OUTPUT_ARGS, call_id="call-1")],
                ),
            ],
        )
        runtime = _make_fake_runtime()
        result = await audit_tools(state, runtime)

        # 终止信号:structured_output 设了
        assert result.get("structured_output") is not None
        assert result["structured_output"].turn_summary == "情绪稳定"
        # M9.5:guidance_injection 强制清掉
        assert result["structured_output"].guidance_injection is None
        # 不发 ToolMessage
        tool_messages = [m for m in result.get("messages", []) if isinstance(m, ToolMessage)]
        assert tool_messages == []
        # 不增 tool_iter_count(终止信号,loop 结束)
        assert "tool_iter_count" not in result

    async def test_single_output_invalid_emits_error_tool_message(self):
        """单 OUTPUT args 非法 → 发 error ToolMessage(带 validation_errors),
        不设 structured_output, tool_iter_count 增 1。"""
        from app.domain.audit.graph import audit_tools
        from langchain_core.messages import ToolMessage

        # crisis_detected=True 但 crisis_topic=None → 触发 _check_crisis_consistency
        bad_args = {
            "dimension_scores": {**_AUDIT_OUTPUT_ARGS["dimension_scores"]},
            "crisis_detected": True,
            "crisis_topic": None,  # 非法搭配
            "redline_triggered": False,
            "redline_detail": None,
            "guidance_injection": None,
            "turn_summary": "x",
        }
        state = _make_state(
            session_notes_working="",
            tool_iter_count=0,
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[_tc(TOOL_NAME_OUTPUT, bad_args, call_id="call-bad")],
                ),
            ],
        )
        runtime = _make_fake_runtime()
        result = await audit_tools(state, runtime)

        import json as _json

        # 不设 structured_output → 路由到 audit_llm_call 修正
        assert "structured_output" not in result
        # 发 1 个 error ToolMessage
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 1
        assert tool_messages[0].tool_call_id == "call-bad"
        payload = _json.loads(tool_messages[0].content)
        assert "error" in payload
        assert "AuditOutputSchema args 校验失败" in payload["error"]
        # 带字段级 validation_errors
        assert "validation_errors" in payload
        assert isinstance(payload["validation_errors"], list)
        assert len(payload["validation_errors"]) > 0
        err0 = payload["validation_errors"][0]
        assert "loc" in err0
        assert "msg" in err0
        assert "type" in err0
        # tool_iter_count 增 1
        assert result["tool_iter_count"] == 1
        # session_notes_working 不变
        assert result["session_notes_working"] == ""

    async def test_single_output_invalid_missing_field(self):
        """单 OUTPUT args 缺字段(dimension_scores 缺失)→ 同样发 error ToolMessage。"""
        from app.domain.audit.graph import audit_tools
        from langchain_core.messages import ToolMessage

        incomplete_args = {
            # 缺 dimension_scores
            "crisis_detected": False,
            "crisis_topic": None,
            "redline_triggered": False,
            "redline_detail": None,
            "guidance_injection": None,
            "turn_summary": "ok",
        }
        state = _make_state(
            session_notes_working="",
            tool_iter_count=2,
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[_tc(TOOL_NAME_OUTPUT, incomplete_args, call_id="call-inc")],
                ),
            ],
        )
        runtime = _make_fake_runtime()
        result = await audit_tools(state, runtime)

        import json as _json

        assert "structured_output" not in result
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 1
        payload = _json.loads(tool_messages[0].content)
        assert "validation_errors" in payload
        # 缺字段错误应指向 dimension_scores
        locs = [tuple(err["loc"]) for err in payload["validation_errors"]]
        assert ("dimension_scores",) in locs


class TestAuditToolsOutputViolation:
    """C3 段:audit_tools 对 OUTPUT 违规 (混调 / 多 OUTPUT) 发 error ToolMessage。"""

    async def test_mixed_output_error(self):
        from app.domain.audit.graph import audit_tools
        from langchain_core.messages import ToolMessage

        state = _make_state(
            session_notes_working="",
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        _tc(TOOL_NAME_APPEND, {"text": "append 内容"}),
                        _tc(TOOL_NAME_OUTPUT, _AUDIT_OUTPUT_ARGS, call_id="call-OUT"),
                    ],
                ),
            ],
        )
        runtime = _make_fake_runtime()
        result = await audit_tools(state, runtime)

        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 2
        # APPEND 是最后 note → 含 current_notes
        append_msg = next(m for m in tool_messages if m.tool_call_id == "call-AppendNote")
        import json as _json

        append_payload = _json.loads(append_msg.content)
        assert append_payload["ok"] is True
        assert "current_notes" in append_payload
        # OUTPUT 违规 → error
        output_msg = next(m for m in tool_messages if m.tool_call_id == "call-OUT")
        output_payload = _json.loads(output_msg.content)
        assert "error" in output_payload
        assert "请单独调用一次 audit_output" in output_payload["error"]

    async def test_multi_output_error(self):
        from app.domain.audit.graph import audit_tools
        from langchain_core.messages import ToolMessage

        state = _make_state(
            session_notes_working="",
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        _tc(TOOL_NAME_OUTPUT, _AUDIT_OUTPUT_ARGS, call_id="call-O1"),
                        _tc(TOOL_NAME_OUTPUT, _AUDIT_OUTPUT_ARGS, call_id="call-O2"),
                    ],
                ),
            ],
        )
        runtime = _make_fake_runtime()
        result = await audit_tools(state, runtime)

        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 2
        import json as _json

        for m in tool_messages:
            payload = _json.loads(m.content)
            assert "error" in payload
            assert "不要与笔记工具混调或重复调用" in payload["error"]


class TestAuditToolsLastNotePayload:
    """C3 段:current_notes 统一在循环底部赋值(失败 OR 末 note)。"""

    async def test_middle_append_no_current_notes(self):
        from app.domain.audit.graph import audit_tools
        from langchain_core.messages import ToolMessage

        state = _make_state(
            session_notes_working="",
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        _tc(TOOL_NAME_APPEND, {"text": "first"}, call_id="call-A1"),
                        _tc(TOOL_NAME_APPEND, {"text": "second"}, call_id="call-A2"),
                    ],
                ),
            ],
        )
        runtime = _make_fake_runtime()
        result = await audit_tools(state, runtime)

        import json as _json

        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 2
        first_payload = _json.loads(tool_messages[0].content)
        second_payload = _json.loads(tool_messages[1].content)
        # 中间成功不附 current_notes,末 note 附
        assert "current_notes" not in first_payload
        assert "current_notes" in second_payload

    async def test_replace_miss_always_has_current_notes(self):
        from app.domain.audit.graph import audit_tools
        from langchain_core.messages import ToolMessage

        state = _make_state(
            session_notes_working="abc",  # 第二个 replace 命中 "b" → "x"
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        _tc(
                            TOOL_NAME_REPLACE,
                            {"old_str": "z", "new_str": "y"},  # miss
                            call_id="call-R1",
                        ),
                        _tc(
                            TOOL_NAME_REPLACE,
                            {"old_str": "b", "new_str": "x"},  # hit
                            call_id="call-R2",
                        ),
                    ],
                ),
            ],
        )
        runtime = _make_fake_runtime()
        result = await audit_tools(state, runtime)

        import json as _json

        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 2
        first_payload = _json.loads(tool_messages[0].content)
        second_payload = _json.loads(tool_messages[1].content)
        # 失败响应附 current_notes
        assert "current_notes" in first_payload
        assert first_payload.get("error") == "old_str not found"
        # 末 note 成功也附 current_notes
        assert "current_notes" in second_payload
        assert second_payload.get("ok") is True


class TestAuditLlmCallTransparent:
    """新设计下 audit_llm_call 是透传节点:有 tool_calls 都不解析,
    structured_output 不设,统一交给 audit_tools 处理。"""

    async def test_single_output_no_parse(self):
        """单 OUTPUT 在 audit_llm_call 透传,structured_output 不设
        (校验 + 终止由 audit_tools 负责)。"""
        state = _make_state()
        runtime = _make_fake_runtime()
        from langchain_core.messages import AIMessage

        with patch("app.domain.audit.graph.build_audit_llm") as mock_build:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(
                return_value=AIMessage(
                    content="",
                    tool_calls=[
                        _tc(TOOL_NAME_OUTPUT, _AUDIT_OUTPUT_ARGS, call_id="call-1"),
                    ],
                )
            )
            mock_build.return_value = mock_llm
            result = await audit_llm_call(state, runtime)

        assert result.get("structured_output") is None
        # 透传 messages
        msgs = result["messages"]
        assert len(msgs) == 1
        last_msg = msgs[-1]
        assert last_msg.tool_calls[0]["name"] == "AuditOutputSchema"

    async def test_note_only_no_structured_output(self):
        state = _make_state()
        runtime = _make_fake_runtime()
        from langchain_core.messages import AIMessage

        with patch("app.domain.audit.graph.build_audit_llm") as mock_build:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(
                return_value=AIMessage(
                    content="",
                    tool_calls=[_tc(TOOL_NAME_APPEND, {"text": "x"}, call_id="call-1")],
                )
            )
            mock_build.return_value = mock_llm
            result = await audit_llm_call(state, runtime)

        assert "structured_output" not in result

    async def test_multi_output_no_parse(self):
        """多 OUTPUT 在 audit_llm_call 透传,交 audit_tools error ToolMessage 路径。"""
        state = _make_state()
        runtime = _make_fake_runtime()
        from langchain_core.messages import AIMessage

        with patch("app.domain.audit.graph.build_audit_llm") as mock_build:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(
                return_value=AIMessage(
                    content="",
                    tool_calls=[
                        _tc(TOOL_NAME_OUTPUT, _AUDIT_OUTPUT_ARGS, call_id="call-1"),
                        _tc(TOOL_NAME_OUTPUT, _AUDIT_OUTPUT_ARGS, call_id="call-2"),
                    ],
                )
            )
            mock_build.return_value = mock_llm
            result = await audit_llm_call(state, runtime)

        assert "structured_output" not in result

    async def test_mixed_output_no_parse(self):
        """混调 [NOTE, OUTPUT] 在 audit_llm_call 透传。"""
        state = _make_state()
        runtime = _make_fake_runtime()
        from langchain_core.messages import AIMessage

        with patch("app.domain.audit.graph.build_audit_llm") as mock_build:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(
                return_value=AIMessage(
                    content="",
                    tool_calls=[
                        _tc(TOOL_NAME_APPEND, {"text": "x"}, call_id="call-1"),
                        _tc(TOOL_NAME_OUTPUT, _AUDIT_OUTPUT_ARGS, call_id="call-2"),
                    ],
                )
            )
            mock_build.return_value = mock_llm
            result = await audit_llm_call(state, runtime)

        assert "structured_output" not in result

    async def test_double_text_fallback_logs_warning(self, caplog):
        """两次 ainvoke 都返回纯文本 → logger.warning 记诊断,
        不设 structured_output(audit_tools 防御性兜底负责设 default)。"""
        state = _make_state()
        runtime = _make_fake_runtime()
        from langchain_core.messages import AIMessage

        with patch("app.domain.audit.graph.build_audit_llm") as mock_build:
            text_response = AIMessage(content="不调工具", tool_calls=[])
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(side_effect=[text_response, text_response])
            mock_build.return_value = mock_llm

            result = await audit_llm_call(state, runtime)

        # 两次 ainvoke 都跑(后处理追问)
        assert mock_llm.ainvoke.await_count == 2
        # 不设 structured_output(audit_tools 兜底)
        assert result.get("structured_output") is None
        # 透传最后一次 response
        msgs = result["messages"]
        assert len(msgs) == 1
        assert msgs[-1].content == "不调工具"
        # 警告日志
        assert any("连续两次未调用 audit_output" in r.message for r in caplog.records)


class TestMaxIterTailFallback:
    """C3 段 max_iter 兜底:扫历史最后 OUTPUT + guidance_injection 强制 None。"""

    async def test_uses_last_output_with_guidance_none(self, caplog):
        from app.domain.audit.graph import audit_tools
        from langchain_core.messages import AIMessage

        # 历史 AIMessage 含 [TC_OUTPUT(guidance="x", crisis=True)]
        # 当前 AIMessage [TC_REPLACE_MISS] 触发 max_iter
        bad_args = {
            "dimension_scores": {**_AUDIT_OUTPUT_ARGS["dimension_scores"]},
            "crisis_detected": True,
            "crisis_topic": "需要关注",  # 合法搭配
            "redline_triggered": False,
            "redline_detail": None,
            "guidance_injection": "x",  # 历史违规残留,期望被强制 None
            "turn_summary": "情绪有波动",
        }
        state = _make_state(
            session_notes_working="",
            tool_iter_count=4,  # 即将到 max_iter=5
            max_iter=5,
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[_tc(TOOL_NAME_OUTPUT, bad_args, call_id="call-hist")],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{**_TC_REPLACE_MISS, "id": "call-now"}],
                ),
            ],
        )
        runtime = _make_fake_runtime()
        result = await audit_tools(state, runtime)

        # structured_output 来自历史最后 OUTPUT
        assert result.get("structured_output") is not None
        # guidance_injection 强制 None(守 M9.5)
        assert result["structured_output"].guidance_injection is None
        # 安全字段保留
        assert result["structured_output"].crisis_detected is True
        assert result["structured_output"].crisis_topic == "需要关注"

    async def test_no_output_falls_back_to_default(self):
        from app.domain.audit.graph import audit_tools
        from langchain_core.messages import AIMessage

        state = _make_state(
            session_notes_working="",
            tool_iter_count=4,
            max_iter=5,
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[{**_TC_REPLACE_MISS, "id": "call-1"}],
                ),
            ],
        )
        runtime = _make_fake_runtime()
        result = await audit_tools(state, runtime)

        assert result.get("structured_output") is not None
        # 无任何 OUTPUT → default fallback
        assert result["structured_output"].guidance_injection is None
        assert result["structured_output"].turn_summary == "审查超时降级"

    async def test_invalid_output_args_falls_back_to_default(self, caplog):
        from app.domain.audit.graph import audit_tools
        from langchain_core.messages import AIMessage

        # 历史 OUTPUT 的 args 非法:crisis_detected=True 但 crisis_topic=None
        bad_args = {
            "dimension_scores": {**_AUDIT_OUTPUT_ARGS["dimension_scores"]},
            "crisis_detected": True,
            "crisis_topic": None,  # 非法:crisis_detected=True 时必须非空
            "redline_triggered": False,
            "redline_detail": None,
            "guidance_injection": None,
            "turn_summary": "x",
        }
        state = _make_state(
            session_notes_working="",
            tool_iter_count=4,
            max_iter=5,
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[_tc(TOOL_NAME_OUTPUT, bad_args, call_id="call-bad")],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{**_TC_REPLACE_MISS, "id": "call-now"}],
                ),
            ],
        )
        runtime = _make_fake_runtime()
        result = await audit_tools(state, runtime)

        # 不抛异常
        assert result.get("structured_output") is not None
        # 校验失败 → default fallback
        assert result["structured_output"].turn_summary == "审查超时降级"
        # warning log
        assert any("max_iter_salvage_validation_failed" in r.message for r in caplog.records)


class TestRouteAfterToolsShortCircuit:
    """C5 段:route_after_tools structured_output 短路,修复 [NOTE, OUTPUT] 回环。"""

    def test_structured_output_short_circuits_to_write_results(self):
        from app.domain.audit.graph import route_after_tools

        # tool_iter_count < max_iter 但 structured_output 已设
        state = _make_state(tool_iter_count=2, max_iter=5, structured_output=MagicMock())
        assert route_after_tools(state) == "write_results"
