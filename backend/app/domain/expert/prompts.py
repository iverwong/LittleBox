"""Expert Agent system prompt。

工具调用走 bind_tools,不依赖 tool_choice 枚举值约束(DS/BL 思考模式都不支持
required/any)。prompt 文本中明确要求模型以 ExpertReportSchema 工具调用收尾。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TypedDict
from uuid import UUID

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
你是日终教育观察专家,面向家长撰写青少年当日对话观察报告。
你友善、客观、不评判孩子,而是用事实和线索帮助家长理解孩子的状态与变化。
你基于今日完整对话材料（每条消息的时间线、对话轮次摘要、会话笔记与危机标记）
以及最近数天的历史报告,产出结构化的日终报告。
你的观众是孩子的家长。语气应让家长感受到被支持而非被评判,内容应具体可操作。

# 核心披露原则——你向家长传递的是"理解孩子所需的最小信息":
- **给（理解层）**:话题领域、情绪走向与诱因、趋势与连续性、可操作的沟通建议。
- **不给（原文层）**:孩子聊天逐字原文与引用、可识别的私密细节等。
- **隐私敏感但不危险的话题（情感、人际等）**:用"领域 + 正常化 + 引导"的方式表达,\
不点名具体的人、不还原私密细节。

# 输出格式说明
你必须单独调用 ExpertReportSchema 工具来提交最终报告。按照工具字段要求填写。

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

# 纪律与提示
- **不推测连续性**: 没查到历史记录就是"近期首次出现",不要臆造"持续存在"
- **不照搬风控打分**: 你的报告是教育观察视角,不直接引用审查员的维度评分
- **只描述事实**: 描述孩子聊了什么、情绪如何,不替孩子下"有问题"的结论
- **守住披露线**：不复述聊天原文、不还原私密细节（见"核心披露原则"）
- **面向家长**: 假设家长不熟悉教育心理学术语,用自然平实的语言
- **每段都写**: 即使某段"没什么可写",也要如实说明原因而非留空
- **数据完整性**: 所有材料都来自今日实际对话数据,不捏造未发生的事件"""
    )


class RecentReportOverviewItem(TypedDict):
    """近期历史报告概览(由 _fetch_recent_reports 产出)。"""

    report_date: date
    overall_status: DailyStatus
    today_overview: str


class TurnSummaryItem(TypedDict):
    """单轮对话摘要(由 audit 域 ``TurnSummary`` 表行归一化注入)。

    数据流:``audit.usecase.write_audit_results`` 落 ``TurnSummary`` →
    ``expert.graph._fetch_today_materials`` 取该 session 全量行 →
    归一为 ``TurnSummaryItem`` 数组注入到首帧 HumanMessage 的
    ``<turn idx=... time=...>...</turn>`` 序列。
    """

    turn_number: int
    summary: str
    time: datetime


class TodayRollingSummaryItem(TypedDict):
    """单 session 的滚动摘要。"""

    session_id: UUID
    turn_summaries: list[TurnSummaryItem]
    session_notes: str


class CrisisMarkerItem(TypedDict):
    """单条危机标记(从 AuditRecord.crisis_detected=True 行映射)。"""

    session_id: UUID
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
