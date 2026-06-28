"""Expert Agent system prompt。

工具调用走 bind_tools,不依赖 tool_choice 枚举值约束(DS/BL 思考模式都不支持
required/any)。prompt 文本中明确要求模型以 ExpertReportSchema 工具调用收尾。
"""

from __future__ import annotations

from datetime import date
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.enums import DailyStatus


def build_expert_system_prompt(max_output_attempts: int) -> SystemMessage:
    """返回日终专家 Agent system prompt。

    Args:
        max_output_attempts: ExpertReportSchema 调用上限。

    Returns:
        构造好的 SystemMessage。
    """
    return SystemMessage(
        content="""\
# 身份与原则
你是日终教育观察专家，面向家长撰写青少年当日 AI 对话的观察报告。
你友善、客观、不评判孩子，而是用事实和线索帮助家长理解孩子的状态与变化。
你基于今日完整对话材料以及相关历史报告，产出结构化的日终报告。
你的观众是孩子的家长。语气应让家长感受到被支持而非被评判，内容应具体可操作。

# 输出格式说明
你必须调用 ExpertReportSchema 工具来提交最终报告。按照工具字段要求填写。报告包含以下 6 个内容段落：

1. **今日概览(today_overview)**: 整体状态一句话概括，如"今天情绪平稳，主要聊了校园生活"
2. **聊了什么(what_was_discussed)**: 今日主要话题与脉络，按时间顺序简述
3. **情绪变化(emotion_changes)**: 情绪波动与诱因描述，如"下午数学题受挫后稍有低落，很快恢复"
4. **值得关注(noteworthy)**: 需要家长留意的观察点，如"提到最近睡眠不太规律"
5. **具体建议(suggestions)**: 给家长的可操作建议，如"可以在晚餐时聊聊今天的美术课作品"
6. **异常时段标注(anomaly_periods)**: 异常时段与具体表现，无异常则写"今日未发现明显异常时段"

# 数据源说明
你看到的材料包括：
- **近期报告概览**: 使用<report>...</report>包裹最近数天的日终报告摘要，\
包含报告日期和报告状态，助你判断连续性与变化
- **今日对话时间线**: 使用<today_summary>...</today_summary>包裹今日每一轮的对话摘要，\
每一轮的对话摘要使用<turn>...</turn>包裹，包含轮次编号和时间信息
- **今日审查笔记**: 使用<note>...</note>包裹审查员维护的跨轮趋势笔记
- **今日危机标记**: 使用<crisis>...</crisis>包裹审查员标记的危机信号（如有）

# 工作流程
1. **先看历史报告**: 了解近期状态基线，识别变化趋势
2. **再看今日材料**: 掌握今日对话全貌
3. **话题回溯**: 根据需要通过调用 SearchHistoryInput 工具搜索特定关键字回溯历史数据
4. **核实原文**: 根据需要通过调用 FetchByRefInput 工具查看完整对话原文或笔记全文
5. **给出报告**: 综合所有信息，调用 ExpertReportSchema 给出最终报告

# 工具使用规范

# 纪律与提示
- **不推测连续性**: 没查到历史记录就是"近期首次出现"，不要臆造"持续存在"
- **不照搬风控打分**: 你的报告是教育观察视角，不直接引用审查员的信息
- **只描述事实**: 描述孩子聊了什么、情绪如何，不替孩子下"有问题"的结论
- **面向家长**: 假设家长不熟悉教育心理学术语，涉及术语时用自然平实的语言说明
- **数据完整性**: 所有材料都来自今日实际对话数据，不捏造未发生的事件
"""
    )


class RecentReportOverviewItem(TypedDict):
    """近期历史报告概览(由 _fetch_recent_reports 产出)。"""

    report_date: date
    overall_status: DailyStatus
    today_overview: str


class TurnSummaryItem(TypedDict):
    """单轮对话摘要(由 ORM 行 turn_summaries JSONB 元素归一化)。"""

    turn_number: int
    summary: str
    time: str


class TodayRollingSummaryItem(TypedDict):
    """单 session 的滚动摘要。"""

    session_id: str
    turn_summaries: list[TurnSummaryItem]
    session_notes: str


class CrisisMarkerItem(TypedDict):
    """单条危机标记(从 AuditRecord.crisis_detected=True 行映射)。"""

    session_id: str
    turn_number: int
    crisis_topic: str


def build_expert_first_human_message(
    report_date: date,
    recent_reports_overview: list[RecentReportOverviewItem],
    today_summary: TodayRollingSummaryItem | None,
    crisis_marker: CrisisMarkerItem | None,
) -> HumanMessage:
    """组装首轮 HumanMessage。

    章节结构(顺序固定,空段跳过):
      1. 报告日期头(恒渲染)
      2. ## 近期历史报告概览(recent_reports_overview 非空时)
      3. ## 今日对话材料(today_rolling_summaries 非空时,内含 session 块)
      4. ## 危机标记(today_rolling_summaries 非空且 crisis_markers 非空时)
    """
    summary_parts: list[str] = []
    note_msg: str | None = None
    crisis_msg: str | None = None
    report_parts = [
        f"<report date={report['report_date'].isoformat()} status={report['overall_status'].value}>\
{report['today_overview']}</report>"
        for report in recent_reports_overview
    ]

    if today_summary:
        for turn in today_summary["turn_summaries"]:
            summary_parts.append(
                f"<turn idx={turn['turn_number']} time={turn['time']}>{turn['summary']}</turn>"
            )
        note_msg = today_summary["session_notes"]

    if crisis_marker:
        crisis_msg = (
            f"<crisis turn={crisis_marker['turn_number']}>{crisis_marker['crisis_topic']}</crisis>"
        )

    return HumanMessage(
        content=f"""\
# 今日日期: {report_date.isoformat()}

---

## 近期报告概览
{"\n".join(report_parts)}

## 今日对话时间线
<today_summary>
{"\n".join(summary_parts)}
</today_summary>

## 今日审查笔记
<note>
{note_msg}
</note>

## 今日危机标记
<crisis>
{crisis_msg}
</crisis>"""
    )
