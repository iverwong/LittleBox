"""build_expert_first_human_message 单元测试。

5 个最小覆盖:空输入 / 仅历史 / 仅今日 / 含危机 / 全量。
无字节等价快照测试(由作者后续改文案,守了反而限制)。
"""

from __future__ import annotations

from datetime import date

from app.core.enums import DailyStatus
from app.domain.expert.prompts import build_expert_first_human_message
from langchain_core.messages import HumanMessage

REPORT_DATE = date(2026, 6, 23)


def test_empty_input_renders_only_date_header():
    """4 个参数全空 → 只渲染报告日期头。"""
    result = build_expert_first_human_message(
        report_date=REPORT_DATE,
        recent_reports_overview=[],
        today_rolling_summaries=[],
        crisis_markers=[],
    )
    assert isinstance(result, HumanMessage)
    assert result.content == f"报告日期: {REPORT_DATE.isoformat()}\n"


def test_history_overview_only_renders_history_section():
    """仅 recent_reports_overview 非空 → 渲染历史段,无今日段。"""
    recent = [
        {
            "report_date": date(2026, 6, 22),
            "overall_status": DailyStatus.stable,
            "today_overview": "正常的一天",
        },
    ]
    result = build_expert_first_human_message(
        report_date=REPORT_DATE,
        recent_reports_overview=recent,
        today_rolling_summaries=[],
        crisis_markers=[],
    )
    assert "## 近期历史报告概览" in result.content
    assert "2026-06-22 [stable]: 正常的一天" in result.content
    assert "## 今日对话材料" not in result.content
    assert "## 危机标记" not in result.content


def test_today_materials_only_renders_today_section():
    """仅 today_rolling_summaries 非空 → 渲染今日段(含 session + notes)。"""
    today = [
        {
            "session_id": "sess-abc",
            "turn_summaries": [
                {"turn_number": 1, "summary": "聊了学校"},
                {"turn_number": 2, "summary": "聊了游戏"},
            ],
            "session_notes": "情绪稳定",
        },
    ]
    result = build_expert_first_human_message(
        report_date=REPORT_DATE,
        recent_reports_overview=[],
        today_rolling_summaries=today,
        crisis_markers=[],
    )
    assert "## 今日对话材料" in result.content
    assert "### Session: sess-abc" in result.content
    assert "- Turn 1: 聊了学校" in result.content
    assert "- Turn 2: 聊了游戏" in result.content
    assert "会话笔记 (sess-abc):" in result.content
    assert "情绪稳定" in result.content
    assert "## 危机标记" not in result.content


def test_crisis_markers_rendered_when_present():
    """crisis_markers 非空 + today_rolling_summaries 也非空 → 危机子段渲染。"""
    today = [
        {
            "session_id": "sess-abc",
            "turn_summaries": [{"turn_number": 1, "summary": "聊了学校"}],
            "session_notes": "",
        },
    ]
    crisis = [
        {"session_id": "sess-abc", "turn_number": 3, "crisis_topic": "情绪低落"},
    ]
    result = build_expert_first_human_message(
        report_date=REPORT_DATE,
        recent_reports_overview=[],
        today_rolling_summaries=today,
        crisis_markers=crisis,
    )
    assert "## 今日对话材料" in result.content
    assert "## 危机标记" in result.content
    assert "- Session sess-abc, Turn 3: 情绪低落" in result.content


def test_full_data_renders_all_four_sections_in_order():
    """全量数据 → 日期头 → 历史 → 今日(含危机) 4 段按固定顺序。"""
    recent = [
        {
            "report_date": date(2026, 6, 22),
            "overall_status": DailyStatus.alert,
            "today_overview": "前一天有危机",
        },
    ]
    today = [
        {
            "session_id": "sess-xyz",
            "turn_summaries": [{"turn_number": 1, "summary": "恢复中"}],
            "session_notes": "",
        },
    ]
    crisis = [
        {"session_id": "sess-xyz", "turn_number": 5, "crisis_topic": "低落"},
    ]
    result = build_expert_first_human_message(
        report_date=REPORT_DATE,
        recent_reports_overview=recent,
        today_rolling_summaries=today,
        crisis_markers=crisis,
    )
    content = result.content
    # 4 段顺序
    assert content.index("报告日期:") < content.index("## 近期历史报告概览")
    assert content.index("## 近期历史报告概览") < content.index("## 今日对话材料")
    assert content.index("## 今日对话材料") < content.index("## 危机标记")
    # 4 段都渲染
    assert "2026-06-22 [alert]: 前一天有危机" in content
    assert "### Session: sess-xyz" in content
    assert "- Turn 1: 恢复中" in content
    assert "- Session sess-xyz, Turn 5: 低落" in content
