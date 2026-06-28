"""Expert 域 Pydantic schemas。

本模块设计为 Pydantic v2 BaseModel 纯数据类,不引入 ORM / LangChain 依赖。
LLM 工具入口用 `SearchHistoryInput` / `FetchByRefInput` 由 LangChain `bind_tools()` 消费,
`ExpertReportSchema` 由 `with_structured_output(include_raw=True)` 消费。
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import DailyStatus

EXPERT_SEARCH_SOURCE_VALUES: tuple[str, ...] = (
    "turn_summary",
    "session_notes",
    "crisis_topic",
    "daily_report",
)
"""Expert 工具支持的 4 类检索数据源(单源,见 SearchHistoryInput.source)。

LLM 想要多源时调多次 SearchHistoryInput,每次一个 source。"""


class SearchHistoryInput(BaseModel):
    """检索历史数据(单源)。

    Attributes:
        keywords: 检索关键词列表,1-8 个,每词至少 2 字符(OR 匹配)。
        source: 单源检索;多源请多次调用。4 类候选见 EXPERT_SEARCH_SOURCE_VALUES。
        start_date: 检索范围起始日期(可选),默认 = end_date - 30 日。
        end_date: 检索范围结束日期(可选),默认 = report_date - 1 日。
        limit: 返回结果上限,1-50 条,默认 15。
        context_chars: 长源开窗字符数,0-400,默认 80。仅对 session_notes /
            daily_report 生效(以匹配位置为中心取前后 N 字符);短源
            (turn_summary / crisis_topic)整段返回。
    """

    keywords: list[str] = Field(
        min_length=1,
        max_length=8,
        description="检索关键词列表,1-8 个,每词至少 2 字符(OR 匹配)",
    )
    source: Literal[
        "turn_summary",
        "session_notes",
        "crisis_topic",
        "daily_report",
    ] = Field(
        ...,
        description="搜索来源。turn_summary:历史轮次会话摘要;session_notes:历史审查笔记;crisis_topic:历史触发的危机主题;daily_report:历史日终专家报告",
    )
    start_date: Optional[date] = Field(
        default=None,
        description="检索范围起始日期(可选),默认 = end_date - 30 日",
    )
    end_date: Optional[date] = Field(
        default=None,
        description="检索范围结束日期(可选),默认 = report_date - 1 日, 不得晚于 start_date 90 天",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="返回结果上限,1-50 条,默认 20",
    )
    context_chars: int = Field(
        default=200,
        ge=0,
        le=400,
        description="长源开窗字符数,0-400,默认 200",
    )

    @field_validator("keywords")
    @classmethod
    def _check_keyword_length(cls, v: list[str]) -> list[str]:
        """校验每个关键词至少 2 字符。"""
        for kw in v:
            if len(kw) < 2:
                raise ValueError(f"关键词长度必须 ≥2,收到 {kw!r}")
        return v


class FetchByRefInput(BaseModel):
    """按引用键获取完整原文。

    ref 格式:
    - `turn:{session_id}#{turn}`: 返回 turn_summary + human/ai 原文 + crisis 标记
    - `notes:{session_id}`: 返回 session_notes 全文 + 元信息
    - `report:{report_id}`: 返回结构化 daily_report dict

    Attributes:
        ref: 引用键字符串,格式如 `turn:uuid#3` / `notes:uuid` / `report:uuid`。
        context_turns: 仅对 `turn:` 类生效,展开前后各 N 轮原文,0-3,默认 0。
    """

    ref: str = Field(
        min_length=1,
        description="""引用键字符串,格式:
- turn:{session_id}#{turn}: 返回 turn_summary + human/ai 原文 + crisis 标记
- notes:{session_id}: 返回 session_notes 全文 + 元信息
- report:{report_id}: 返回结构化 daily_report dict""",
    )
    context_turns: int = Field(
        default=0,
        ge=0,
        le=3,
        description="仅对 turn: 类生效,展开前后各 N 轮原文,0-3,默认 0",
    )


class ExpertReportSchema(BaseModel):
    """日终专家报告结构化输出。

    LLM 综合当日对话材料、历史报告与检索结果,产出 6 段内容的报告。
    所有字符串字段均需填写,不留空段。
    """

    overall_status: DailyStatus = Field(
        description="""当日整体状态:
- stable: 平稳,无明显风险信号
- attention: 关注,出现需留意的观察
- alert: 警示,触发危机/连续高分维度""",
    )
    degraded: bool = Field(
        default=False,
        description="是否为降级产物(True 表示 max_iter 超限/LLM 未调输出工具)",
    )
    today_overview: str = Field(
        min_length=1,
        description="1. 今日概览:整体状态一句话概括",
    )
    what_was_discussed: str = Field(
        min_length=1,
        description="2. 聊了什么:今日主要话题与脉络",
    )
    emotion_changes: str = Field(
        min_length=1,
        description="3. 情绪变化:情绪波动与诱因描述",
    )
    noteworthy: str = Field(
        min_length=1,
        description="4. 值得关注:需要家长留意的观察点",
    )
    suggestions: str = Field(
        min_length=1,
        description="5. 具体建议:给家长的可操作建议",
    )
    anomaly_periods: str = Field(
        min_length=1,
        description="6. 异常时段标注:异常时段与具体表现",
    )


class DailyDimensionSummary(BaseModel):
    """6 维度当日聚合 peak / mean / high_ratio。

    代码层从 `audit_records.dimension_scores` 聚合,供 UI 雷达图与跨日对比使用。
    写入路径见 `app.domain.expert.worker._aggregate_dimensions`。

    `frozen=True` 防止原地修改绕过 SQLAlchemy 脏检测。

    Attributes:
        peak: 6 维度当日最高分(0-9)。
        mean: 6 维度当日平均分(0-9)。
        high_ratio: 6 维度中进入高分(≥4)轮次占比(0-1)。
    """

    model_config = ConfigDict(frozen=True)

    peak: float = Field(ge=0, le=9)
    mean: float = Field(ge=0, le=9)
    high_ratio: float = Field(ge=0, le=1)
