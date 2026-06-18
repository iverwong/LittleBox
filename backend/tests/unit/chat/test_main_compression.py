"""Unit tests for build_messages_main compression path.

Tests cover both compressed and non-compressed paths.
Does NOT test integration — all DB/LLM calls are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from langchain_core.messages import HumanMessage, SystemMessage


class TestBuildMessagesMainCompression:
    """build_messages_main 压缩路径单元测试。

    验证：
    - needs_compression=True → 调 _handle_compress + 发 compression_start/end
    - needs_compression=False → 跳过压缩
    - guidance 正确传给 format_guidance_wrapper
    - 返回 messages 结构: [system_prompt, *history, HumanMessage(wrapped)]
    """

    @pytest.fixture
    def ctx(self):
        ctx = MagicMock()
        ctx.session_id = UUID("00000000-0000-0000-0000-000000000001")
        ctx.child_profile = MagicMock()
        ctx.user_input = "测试消息"
        return ctx

    @pytest.fixture
    def state(self):
        return {
            "turn_number": 3,
            "audit_state": {"guidance": None},
            "messages": [],
        }

    @pytest.fixture
    def mock_orm_msg(self):
        """创建 mock ORM Message 对象（to_lc_message 消费的 role / content / id）。"""
        m = MagicMock()
        m.role = "human"
        m.content = "历史消息"
        m.id = UUID("00000000-0000-0000-0000-000000000002")
        return m

    async def test_no_compression_skips_compress(self, ctx, state, mock_orm_msg):
        """needs_compression=False → 跳过压缩，直接装配 messages。"""
        mock_db = AsyncMock()
        mock_db.scalar = AsyncMock(return_value=False)
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_db
        ctx.db_session_factory = MagicMock(return_value=mock_cm)

        summary_mock = MagicMock()
        summary_mock.content = "旧摘要"

        with (
            patch(
                "app.domain.chat.graph.load_active_messages_with_summary",
                AsyncMock(return_value=([mock_orm_msg], summary_mock)),
            ),
            patch("app.domain.chat.graph.get_stream_writer") as mock_writer_fn,
            patch(
                "app.domain.chat.graph.build_system_prompt",
                return_value=SystemMessage(content="sys"),
            ) as mock_sp,
            patch(
                "app.domain.chat.graph.format_guidance_wrapper",
                return_value="wrapped: input",
            ),
        ):
            mock_writer = MagicMock()
            mock_writer_fn.return_value = mock_writer

            from app.domain.chat.graph import build_messages_main

            runtime = MagicMock()
            runtime.context = ctx

            result = await build_messages_main(state, runtime)

        # 不发射 compression 帧
        assert mock_writer.call_count == 0
        # build_system_prompt 接收旧摘要
        mock_sp.assert_called_once_with(ctx.child_profile, "旧摘要")
        # messages: [system, history_human, wrapped_human]
        assert len(result["messages"]) == 3
        assert isinstance(result["messages"][0], SystemMessage)
        assert isinstance(result["messages"][1], HumanMessage)
        assert isinstance(result["messages"][2], HumanMessage)

    async def test_compression_calls_handle_compress(
        self, ctx, state, mock_orm_msg
    ):
        """needs_compression=True → 调 _handle_compress + 发 compression_start/end。"""
        mock_db = AsyncMock()
        mock_db.scalar = AsyncMock(return_value=True)
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_db
        ctx.db_session_factory = MagicMock(return_value=mock_cm)

        compress_orm = [MagicMock(), MagicMock()]
        keep_orm = [mock_orm_msg]
        for mo in compress_orm:
            mo.role = "human"
            mo.content = "待压缩"
            mo.id = UUID("00000000-0000-0000-0000-000000000003")
        summary_mock = MagicMock()
        summary_mock.content = "旧摘要"

        with (
            patch(
                "app.domain.chat.graph.load_active_messages_with_summary",
                AsyncMock(
                    return_value=(
                        [*compress_orm, *keep_orm],
                        summary_mock,
                    )
                ),
            ),
            patch(
                "app.domain.chat.graph.split_for_compression",
                return_value=(compress_orm, keep_orm),
            ),
            patch(
                "app.domain.chat.graph._handle_compress",
                AsyncMock(return_value="新摘要"),
            ) as mock_compress,
            patch("app.domain.chat.graph.get_stream_writer") as mock_writer_fn,
            patch(
                "app.domain.chat.graph.build_system_prompt",
                return_value=SystemMessage(content="sys"),
            ) as mock_sp,
            patch(
                "app.domain.chat.graph.format_guidance_wrapper",
                return_value="wrapped: input",
            ),
        ):
            mock_writer = MagicMock()
            mock_writer_fn.return_value = mock_writer

            from app.domain.chat.graph import build_messages_main

            runtime = MagicMock()
            runtime.context = ctx

            result = await build_messages_main(state, runtime)

        # 发射 compression_start + compression_end
        assert mock_writer.call_count == 2
        mock_writer.assert_any_call({"compression_start": {}})
        mock_writer.assert_any_call({"compression_end": {}})

        # _handle_compress 被调一次
        mock_compress.assert_awaited_once()

        # build_system_prompt 接收新摘要
        mock_sp.assert_called_once_with(ctx.child_profile, "新摘要")

        # messages: [system, *keep_orm, wrapped_human]
        assert len(result["messages"]) == 1 + len(keep_orm) + 1
        assert isinstance(result["messages"][0], SystemMessage)
        assert isinstance(result["messages"][-1], HumanMessage)

    async def test_compression_with_guidance_passes_to_wrapper(self, ctx):
        """guidance 非空时传给 format_guidance_wrapper。"""
        state_w_guidance = {
            "turn_number": 3,
            "audit_state": {"guidance": "安全提醒"},
            "messages": [],
        }
        mock_db = AsyncMock()
        mock_db.scalar = AsyncMock(return_value=False)
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_db
        ctx.db_session_factory = MagicMock(return_value=mock_cm)

        with (
            patch(
                "app.domain.chat.graph.load_active_messages_with_summary",
                AsyncMock(return_value=([], None)),
            ),
            patch("app.domain.chat.graph.get_stream_writer"),
            patch(
                "app.domain.chat.graph.build_system_prompt",
                return_value=SystemMessage(content="sys"),
            ),
            patch(
                "app.domain.chat.graph.format_guidance_wrapper",
                return_value="wrapped",
            ) as mock_fmt,
        ):
            from app.domain.chat.graph import build_messages_main

            runtime = MagicMock()
            runtime.context = ctx

            _ = await build_messages_main(state_w_guidance, runtime)

        mock_fmt.assert_called_once_with("测试消息", "安全提醒")
