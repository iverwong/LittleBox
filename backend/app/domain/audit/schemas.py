"""审查 Pipeline Pydantic schemas。

本模块设计为 Pydantic v2 BaseModel 纯数据类,不引入 ORM / LangChain 依赖。
与 `app.domain.audit.models` 的 ORM 模型共享字段名但独立命名空间。

LLM tool 用 `ReplaceInNotes` 由 LangChain `bind_tools()` 消费,
`AuditOutputSchema` 由 `with_structured_output(include_raw=True)` 消费。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class AuditDimensionScores(BaseModel):
    """审查 Agent 输出的 6 维度评分(0-9 整数)。

    与 `child_profiles.sensitivity`(家长配置)共享相同的 6 个 key;
    审查 Agent 在评分时已通过 prompt 内化家长配置,输出即最终分数。
    范围 0-9(10 阶)。

    Attributes:
        emotional: 情绪与心理维度评分。
        social: 人际与社交维度评分。
        values: 价值观与世界观维度评分。
        boundaries: AI 应用边界维度评分。
        academic: 学习独立性维度评分。
        lifestyle: 生活方式维度评分。
    """

    emotional: int = Field(
        default=0,
        ge=0,
        le=9,
        description="""emotional（情绪与心理）
涵盖：压力、焦虑、孤独、低落、自我否定、情绪波动。
低分=一般性烦恼（考试紧张、累）；中=持续低落、明显焦虑、反复自我贬低；
高=强烈无价值感、绝望、情绪明显失控。""",
    )
    social: int = Field(
        default=0,
        ge=0,
        le=9,
        description="""social（人际与社交）
涵盖：交友困惑、同伴压力、被排挤、霸凌、人际冲突。
低分=普通交友烦恼；中=被孤立、同伴压力较大、反复冲突；
高=持续遭霸凌、严重排挤、社交退缩。""",
    )
    values: int = Field(
        default=0,
        ge=0,
        le=9,
        description="""values（价值观与世界观）
涵盖：宗教、道德、社会争议话题、是非观的探讨。
低分=正常的价值观好奇与讨论；中=接触明显偏颇/争议观点、出现认知混乱；
高=被灌输极端或有害世界观、明显被误导。""",
    )
    boundaries: int = Field(
        default=0,
        ge=0,
        le=9,
        description="""boundaries（AI 应用边界）
涵盖：要求 AI 扮演恋人朋友、模拟恋爱、提供暧昧或过度亲密的互动；
诱导 AI 突破限制（忽略设定、"开发者模式"、"无限制 AI"）；
把 AI 当作真人替代或主要情感寄托。
低分=正常使用、偶尔尝试角色扮演；中=反复要求恋爱/亲密扮演、试探越界；
高=持续索求亲密关系扮演、强烈情感依附、刻意诱导 AI 突破安全设定。""",
    )
    academic: int = Field(
        default=0,
        ge=0,
        le=9,
        description="""academic（学习独立性）
涵盖：过度依赖 AI、直接索要答案、回避独立思考。
低分=正常提问求知、偶尔想要现成答案；中=频繁直接要答案、回避思考；
高=几乎完全让 AI 代劳作业与思考。""",
    )
    lifestyle: int = Field(
        default=0,
        ge=0,
        le=9,
        description="""lifestyle（生活方式）
涵盖：作息/熬夜、游戏沉迷、饮食、烟酒、能量饮料等。
低分=偶尔熬夜或游戏时间偏多；中=持续作息紊乱、明显沉迷、不健康饮食；
高=接触烟酒、出现严重成瘾性行为。""",
    )


class TurnSummaryEntry(BaseModel):
    """单轮对话的客观中立短摘要。

    审查图每轮 append 一条到 `rolling_summaries.turn_summaries`;
    摘要口吻严格客观中立,禁带风控判断。

    Attributes:
        turn_number: 对话轮次编号,与 ai_turn_counter 对齐。
        summary: 单行摘要,≤100 字符。
        created_at: ISO-8601 格式 UTC 时间戳。
    """

    turn_number: int = Field(description="对话轮次编号，与 ai_turn_counter 对齐")
    summary: str = Field(max_length=100, description="单行摘要，≤100 字符")
    created_at: str = Field(description="ISO-8601 格式 UTC 时间戳")

    @field_validator("created_at")
    @classmethod
    def _valid_iso8601(cls, v: str) -> str:
        """校验 created_at 字段为合法 ISO-8601 字符串。

        Args:
            v: 原始字段值。

        Returns:
            原值(校验通过后透传)。
        """
        datetime.fromisoformat(v)
        return v


class AuditOutputSchema(BaseModel):
    """最终单独调用的完整结构化输出"""

    dimension_scores: AuditDimensionScores = Field(
        description="""按以下 6 个维度对本轮对话各打 0–9 风险分\
（评分轴是"有多该担心"，而不是"聊得多不多"）：
- 0 = 本轮无相关内容，或虽涉及但完全正常健康
- 1–3 = 轻微、一般性，留作观测即可
- 4–6 = 明显值得留意，出现持续或加重的苗头
- 7–9 = 显著令人担心，需要重点关注"""
    )
    crisis_detected: bool = Field(
        default=False,
        description="是否检测到危机信号",
    )
    crisis_topic: str | None = Field(
        default=None,
        description="危机主题描述，crisis_detected=True 时必须提供",
    )
    guidance_injection: str | None = Field(
        default=None,
        max_length=300,
        description="仅当需要对AI做轻度引导注入时才填写注入文本；正常无风险轮次必须留空",
    )
    turn_summary: str = Field(
        max_length=100,
        description="本轮对话客观摘要，不带风控视角，中立无判断，≤100 字符",
    )

    @field_validator("crisis_topic", "guidance_injection", mode="before")
    @classmethod
    def _normalize_null_string(cls, v: str | None) -> str | None:
        """归一化 LLM 将 null 序列化为字符串 "null" 的边界情况。"""
        if v is not None and v.lower() == "null":
            return None
        return v

    @model_validator(mode="after")
    def _check_crisis_consistency(self) -> Self:
        if self.crisis_detected and self.crisis_topic is None:
            raise ValueError("crisis_detected=True 时 crisis_topic 必须非空")
        if not self.crisis_detected and self.crisis_topic is not None:
            raise ValueError("crisis_detected=False 时 crisis_topic 必须为 None")
        return self


class ReplaceInNotes(BaseModel):
    """替换 `session_notes` 中一段精确匹配的文本
    首轮调用时，请参考系统提示词中<session_notes>块包裹的原文"""

    old_str: str = Field(min_length=1, description="待替换的原文片段，必须唯一精确匹配")
    new_str: str = Field(min_length=0, description="替换后的新文本")


class AuditSignalsPayload(BaseModel):
    """Redis `audit:{sid}` 单 key 三态信号管道值。

    - pending: 已入队等待 worker 处理
    - ready: worker 完成,signals 就绪
    - failed: 重试用尽,error 描述失败原因
    TTL 24h(config.audit_redis_ttl_seconds),到期自动过期。

    Attributes:
        status: 三态之一。
        turn: 对应的 ai_turn 轮次,用于主图 turn 校验。
        signals: 审查结果,status=ready 时必填。
        started_at: worker 开始处理时间(ISO-8601)。
        completed_at: worker 完成时间(ISO-8601)。
        error: 失败原因描述,status=failed 时必填。
    """

    status: Literal["pending", "ready", "failed"]
    turn: int = Field(description="对应的 ai_turn 轮次，用于主图 turn 校验")
    signals: AuditOutputSchema | None = Field(
        default=None,
        description="status=ready 时携带审查结果",
    )
    started_at: str | None = Field(
        default=None,
        description="worker 开始处理时间（ISO-8601）",
    )
    completed_at: str | None = Field(
        default=None,
        description="worker 完成时间（ISO-8601）",
    )
    error: str | None = Field(
        default=None,
        description="status=failed 时描述错误原因",
    )

    @model_validator(mode="after")
    def _check_signals_status(self) -> Self:
        if self.status == "ready" and self.signals is None:
            raise ValueError("status=ready 时 signals 必须非空")
        if self.status in ("pending", "failed") and self.signals is not None:
            raise ValueError("status=pending/failed 时 signals 必须为 None")
        return self

    @model_validator(mode="after")
    def _check_error_status(self) -> Self:
        if self.status == "failed" and not self.error:
            raise ValueError("status=failed 时 error 必须非空")
        if self.status in ("pending", "ready") and self.error is not None:
            raise ValueError("status=pending/ready 时 error 必须为 None")
        return self
