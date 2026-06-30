"""Expert 域 Pydantic schemas。

本模块设计为 Pydantic v2 BaseModel 纯数据类,不引入 ORM / LangChain 依赖。
LLM 工具入口用 `SearchHistoryInput` / `FetchByRefInput` 由 LangChain `bind_tools()` 消费,
`ExpertReportSchema` 由 `with_structured_output(include_raw=True)` 消费。
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import DailyStatus, MessageRole


class SearchSourceType(StrEnum):
    """Expert 工具检索数据源枚举。

    由 ``SearchHistoryInput.source`` 与 ``FetchByRefInput.search_source`` 共同消费。
    LLM 想多源检索时调多次 ``SearchHistoryInput``,每次传一个 source。
    """

    TURN_SUMMARY = "turn_summary"  # 历史轮次会话摘要(audit.TurnSummary 表)
    SESSION_NOTES = "session_notes"  # 历史会话审查笔记(audit.RollingSummary.session_notes)
    CRISIS_TOPIC = "crisis_topic"  # 历史轮次危机触发主题(audit.AuditRecord.crisis_topic)
    DAILY_REPORT = "daily_report"  # 历史会话日终专家报告(expert.DailyReport)


class SearchHistoryInput(BaseModel):
    """当现有信息无法支撑报告连续性和脉络完整性时调用。通过对指定来源进行检索，获取命中关键字的相关信息"""

    source: SearchSourceType = Field(
        ...,
        description="""搜索来源
- turn_summary:历史轮次会话摘要
- session_notes:历史会话审查笔记
- crisis_topic:历史轮次危机触发主题
- daily_report:历史会话日终专家报告
        """,
    )
    keywords: list[str] = Field(
        min_length=1,
        max_length=8,
        description="检索关键词列表,每词至少 2 字符(OR 匹配)",
    )
    start_date: date | None = Field(
        default=None,
        description="检索范围起始日期(可选),默认 = end_date - 30 日",
    )
    end_date: date | None = Field(
        default=None,
        description="检索范围结束日期(可选),默认 = report_date - 1 日, 不得晚于 start_date 90 天",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="返回结果上限",
    )
    context_chars: int = Field(
        default=200,
        ge=0,
        le=400,
        description="长源开窗字符数,仅 session_notes, daily_report 有效",
    )

    @field_validator("keywords")
    @classmethod
    def _check_keyword_length(cls, v: list[str]) -> list[str]:
        """关键字预处理:剔除空串 + 去前后空白 + 长度校验 + 去重。

        Pydantic 校验顺序:
        1. ``min_length=1`` / ``max_length=8`` 先抛 list 长度错误;
        2. 本 validator 兜每条 entry 的字符级约束。
        """
        # 1. 过滤空串 / 纯空白条目,空字符串单独 raise(语义比"长度不足 2"更清晰)
        cleaned: list[str] = []
        for k in v:
            if not isinstance(k, str):
                raise ValueError(f"关键词必须为字符串,收到 {type(k).__name__}")
            stripped = k.strip()
            if not stripped:
                raise ValueError("关键词不能为空或纯空白")
            cleaned.append(stripped)
        if not cleaned:
            raise ValueError("keywords 不能为空")
        # 2. 校验每个非空关键词至少 2 字符(在 stripped 后做,避免"  a"绕过)
        for kw in cleaned:
            if len(kw) < 2:
                raise ValueError(f"关键词长度必须 ≥2,收到 {kw!r}")
        # 3. 去重但保持首次出现顺序(``dict.fromkeys`` 语义)
        return list(dict.fromkeys(cleaned))


class FetchByRefInput(BaseModel):
    """当需要历史数据源完整信息时调用。按 ``(search_source, ref)`` 取完整原文。

    ``ref`` 为数据源主键的 UUID,与 ``search_source`` 一一对应:
    - turn_summary → audit.TurnSummary.id
    - session_notes → audit.RollingSummary.id
    - crisis_topic → audit.AuditRecord.id
    - daily_report → expert.DailyReport.id

    LLM 应先用 ``SearchHistoryInput`` 检索,从 ``MatchItem.ref`` 直接回带;
    该键的物理含义对 LLM 透明,只需原样转发。
    """

    search_source: SearchSourceType = Field(
        ...,
        description="""引用来源
- turn_summary, crisis_topic:获取检索结果中定位轮次的完整对话信息
- session_notes:获取检索结果中定位会话的完整审查笔记信息
- daily_report:获取检索结果中定位会话的完整日终专家报告
""",
    )
    ref: UUID = Field(
        ...,
        description="数据源引用, 通过 SearchHistoryInput 检索后获取",
    )
    context_turns: int = Field(
        default=0,
        ge=0,
        le=3,
        description="仅对 turn_summary, crisis_topic 源生效,返回展开前后各 N 轮原文对话",
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


class MatchItem(TypedDict):
    """检索后匹配的数据
    TypedDict
    """

    ref: UUID
    source: SearchSourceType
    snippet: str
    occurred_at: datetime | None
    locating: str


class SearchResult(TypedDict):
    """检索工具的返回结果
    TypedDict
    """

    has_more: bool
    match_list: list[MatchItem]


class FetchMessageResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: UUID
    turn_number: int
    role: MessageRole
    content: str
    created_at: datetime


class FetchRollingSummaryResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: UUID
    session_notes: str | None
    updated_at: datetime


class FetchDailyReportResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: UUID
    report_date: date
    degraded: bool
    overall_status: DailyStatus
    today_overview: str
    what_was_discussed: str
    emotion_changes: str
    noteworthy: str
    suggestions: str
    anomaly_periods: str
