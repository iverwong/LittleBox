"""Expert worker 测试：run_daily_reports happy path / 失败隔离。

使用 mock RuntimeResources 和 fake expert_graph 实现隔离。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = [
    pytest.mark.asyncio,
]

CUID_1 = uuid.uuid4()
CUID_2 = uuid.uuid4()
SID_1 = uuid.uuid4()
SID_2 = uuid.uuid4()
REPORT_DATE = date(2026, 6, 22)  # run at 04:05 Shanghai on 06-23 → report_date = 06-22

from app.core.time import SHANGHAI


def _make_mock_arq_ctx(
    *,
    expert_graph_result: dict | None = None,
    expert_graph_side_effect: Exception | None = None,
    children: list[uuid.UUID] | None = None,
    crisis_today: bool = False,
    today_session_count: int = 1,
) -> dict:
    """构造 mock ARQ ctx 字典。

    Args:
        today_session_count: 当日 session 数量,worker 3 路处理:
          0 → 跳过该 child(产品逻辑"有聊才有报")
          1 → 正常路径,取 session.id
          ≥2 → fail loud,被 return_exceptions=True 兜住
    """
    if expert_graph_result is None:
        expert_graph_result = {"structured_output": MagicMock()}
    if children is None:
        children = [CUID_1]

    mock_graph = AsyncMock()
    if expert_graph_side_effect:
        mock_graph.ainvoke.side_effect = expert_graph_side_effect
    else:
        mock_graph.ainvoke.return_value = expert_graph_result

    mock_rr = MagicMock()
    mock_rr.settings.expert_max_concurrent_children = 2
    mock_rr.settings.expert_token_budget = 100_000
    mock_rr.expert_graph = mock_graph

    # 构造 mock DB session
    mock_db = AsyncMock()

    async def _mock_execute(stmt, params=None, **kwargs):
        _ = kwargs
        sql_str = str(stmt)
        result = MagicMock()

        if "FROM users" in sql_str and "child_profiles" in sql_str:
            # 孩子列表查询（ORM select(User).join(ChildProfile)）
            mock_users = []
            for cid in children:
                u = MagicMock()
                u.id = cid
                mock_users.append(u)
            result.scalars.return_value.all.return_value = mock_users
        elif "FROM sessions" in sql_str and "WHERE sessions" in sql_str and "created_at" in sql_str:
            # 当日 Session 查询（ORM select(Session)）,按 today_session_count 控 3 路
            today_sessions: list[MagicMock] = []
            for i in range(today_session_count):
                s = MagicMock()
                s.id = uuid.uuid4()
                today_sessions.append(s)
            result.scalars.return_value.all.return_value = today_sessions
            # worker 用 scalar_one_or_none() 取单条,同步设
            result.scalar_one_or_none.return_value = (
                today_sessions[0] if today_sessions else None
            )
        elif "FROM sessions" in sql_str and "WHERE sessions" in sql_str:
            # owned_session_ids 查询（ORM select(Session.id)）
            result.scalars.return_value.all.return_value = [SID_1]
        elif "FROM child_profiles" in sql_str and "WHERE child_profiles" in sql_str:
            # ChildProfile 查询（ORM select(ChildProfile)）
            profile = MagicMock()
            profile.child_user_id = CUID_1
            profile.nickname = "test"
            profile.gender = MagicMock()
            profile.gender.value = "male"
            profile.birth_date = date(2015, 1, 1)
            profile.sensitivity = None
            profile.custom_redlines = None
            profile.concerns = None
            result.scalars.return_value.first.return_value = profile
            # worker 用 scalar_one_or_none() 取单条,同步设
            result.scalar_one_or_none.return_value = profile
        elif "dimension_scores" in sql_str:
            # 维度聚合查询（ORM select(AuditRecord.dimension_scores)）
            result.scalars.return_value.all.return_value = []
        elif "FROM daily_reports" in sql_str:
            # 历史报告查询（ORM select(DailyReport)）
            result.scalars.return_value.all.return_value = []

        return result

    mock_db.execute = _mock_execute
    # crisis check 使用 db.scalar()，单独 mock
    mock_db.scalar = AsyncMock(return_value=crisis_today)
    # 兼容 db.scalar() 和 db.execute() 两种调用方式
    # （_check_crisis_today 使用 db.scalar，其余使用 db.execute）

    db_cm = AsyncMock()
    db_cm.__aenter__.return_value = mock_db
    mock_rr.db_session_factory.return_value = db_cm

    return {
        "resources": mock_rr,
        "redis": MagicMock(),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunDailyReports:
    """run_daily_reports worker 测试。"""

    async def test_happy_path(self):
        """Happy path：无孩子 → 直接返回。"""
        from app.domain.expert.worker import run_daily_reports

        ctx = _make_mock_arq_ctx(children=[])
        # 无活跃孩子 → 不调 expert_graph，直接返回
        await run_daily_reports(ctx)

    async def test_happy_path_with_children(self):
        """有活跃孩子 → 调用 expert_graph.ainvoke。"""
        from app.domain.expert.worker import run_daily_reports

        ctx = _make_mock_arq_ctx(children=[CUID_1])

        with patch(
            "app.domain.expert.worker.now_shanghai",
            return_value=datetime(REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day + 1, 4, 5, tzinfo=SHANGHAI),
        ):
            await run_daily_reports(ctx)

        ctx["resources"].expert_graph.ainvoke.assert_called()

    async def test_child_failure_isolation(self):
        """一个孩子失败不应影响另一个孩子。"""
        from app.domain.expert.worker import run_daily_reports

        children = [CUID_1, CUID_2]
        ctx = _make_mock_arq_ctx(
            children=children,
            expert_graph_side_effect=RuntimeError("模拟失败"),
        )

        with patch(
            "app.domain.expert.worker.now_shanghai",
            return_value=datetime(REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day + 1, 4, 5, tzinfo=SHANGHAI),
        ):
            # 不应抛出异常（return_exceptions=True）
            await run_daily_reports(ctx)

        # 两个孩子的 graph.ainvoke 都应被调过
        assert ctx["resources"].expert_graph.ainvoke.await_count == 2

    async def test_no_active_children(self):
        """无活跃孩子 → 不调 expert_graph。"""
        from app.domain.expert.worker import run_daily_reports

        ctx = _make_mock_arq_ctx(children=[])
        await run_daily_reports(ctx)

        ctx["resources"].expert_graph.ainvoke.assert_not_called()

    async def test_zero_today_session_skips_child(self):
        """当日 0 session → 跳过该 child,不调 expert_graph。"""
        from app.domain.expert.worker import run_daily_reports

        ctx = _make_mock_arq_ctx(children=[CUID_1], today_session_count=0)
        with patch(
            "app.domain.expert.worker.now_shanghai",
            return_value=datetime(REPORT_DATE.year, REPORT_DATE.month, REPORT_DATE.day + 1, 4, 5, tzinfo=SHANGHAI),
        ):
            await run_daily_reports(ctx)

        ctx["resources"].expert_graph.ainvoke.assert_not_called()
