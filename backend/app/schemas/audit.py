"""审查 Pipeline Pydantic schemas（M8）。

本模块设计为 Pydantic v2 BaseModel 纯数据类，不引入 ORM / LangChain 依赖。
与 `app.models.audit` 的 ORM 模型共享字段名但独立命名空间。

LLM tool 用 `AppendNote` / `ReplaceInNotes` 由 LangChain `bind_tools()` 消费，
`AuditOutputSchema` 由 `with_structured_output(include_raw=True)` 消费。
详见 D11 决议：with_structured_output + bind_tools 同帧调用兼容性由 Step 4 live spike 验证。

TODO(M9+): provider 切换时补充英文 Field(description=...) 实现双语兼容；
当前 DeepSeek 对中文 description 理解正常。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class AuditDimensionScores(BaseModel):
    """审查 Agent 输出的 7 维度评分。

    与 `child_profiles.sensitivity`（家长配置）共享相同的 7 个 key；
    审查 Agent 在评分时已通过 prompt 内化家长配置，输出即最终分数。
    范围 0-9（10 阶），与架构基线 §四/七/九 保持一致。
    """

    emotional: int = Field(default=0, ge=0, le=9, description="情绪维度评分")
    social: int = Field(default=0, ge=0, le=9, description="社交维度评分")
    romance: int = Field(default=0, ge=0, le=9, description="情感维度评分")
    values: int = Field(default=0, ge=0, le=9, description="价值观维度评分")
    boundaries: int = Field(default=0, ge=0, le=9, description="边界感维度评分")
    academic: int = Field(default=0, ge=0, le=9, description="学业维度评分")
    lifestyle: int = Field(default=0, ge=0, le=9, description="生活方式维度评分")


class TurnSummaryEntry(BaseModel):
    """单轮对话的客观中立短摘要。

    审查图每轮 append 一条到 `rolling_summaries.turn_summaries`；
    供主对话图超滑窗后的上下文压缩。
    摘要口吻严格客观中立，禁带风控判断。
    """

    turn_number: int = Field(description="对话轮次编号，与 ai_turn_counter 对齐")
    summary: str = Field(max_length=100, description="单行摘要，≤100 字符")
    created_at: str = Field(description="ISO-8601 格式 UTC 时间戳")

    @field_validator("created_at")
    @classmethod
    def _valid_iso8601(cls, v: str) -> str:
        datetime.fromisoformat(v)
        return v


class AuditOutputSchema(BaseModel):
    """审查 Agent 一次调用的完整结构化输出。

    8 字段含 7 维度评分、危机/红线信号、家长引导建议、客观摘要。
    由 `with_structured_output(AuditOutputSchema, include_raw=True)` 消费。
    """

    dimension_scores: AuditDimensionScores = Field(
        description="7 维度评分（0-9）"
    )
    crisis_detected: bool = Field(
        default=False,
        description="是否检测到危机信号（自残/自杀/虐待等紧急情况）",
    )
    crisis_topic: str | None = Field(
        default=None,
        description="危机主题描述，crisis_detected=True 时必须提供",
    )
    redline_triggered: bool = Field(
        default=False,
        description="是否触发红线（色情/暴力/违法内容等违规行为）",
    )
    redline_detail: str | None = Field(
        default=None,
        description="红线触发详情，redline_triggered=True 时必须提供",
    )
    guidance: str = Field(
        default="",
        max_length=300,
        description="家长引导建议，≤300 字符；M8 期透传，M9 接 inject_guidance 节点真消费",
    )
    turn_summary: str = Field(
        max_length=100,
        description="本轮对话客观摘要，≤100 字符；与 TurnSummaryEntry.summary 语义一致",
    )

    @model_validator(mode="after")
    def _check_crisis_consistency(self) -> Self:
        if self.crisis_detected and self.crisis_topic is None:
            raise ValueError("crisis_detected=True 时 crisis_topic 必须非空")
        if not self.crisis_detected and self.crisis_topic is not None:
            raise ValueError("crisis_detected=False 时 crisis_topic 必须为 None")
        return self

    @model_validator(mode="after")
    def _check_redline_consistency(self) -> Self:
        if self.redline_triggered and self.redline_detail is None:
            raise ValueError("redline_triggered=True 时 redline_detail 必须非空")
        if not self.redline_triggered and self.redline_detail is not None:
            raise ValueError("redline_triggered=False 时 redline_detail 必须为 None")
        return self


class AppendNote(BaseModel):
    """在 `session_notes` 末尾追加一段文本。

    每次调用返回当前完整 notes 全文。LLM 不自推导 notes 现态。
    """

    text: str = Field(
        min_length=1,
        max_length=500,
        description="追加内容，≤500 字符；LLM 不自推导当前 notes，必须读取返回值",
    )


class ReplaceInNotes(BaseModel):
    """替换 `session_notes` 中一段精确匹配的文本。

    唯一匹配语义（大小写敏感）：
    - 0 命中 → 不修改，返 `{"ok": false, "error": "old_str not found"}`
    - 1 命中 → 替换并返 `{"ok": true, "current_notes": "..."}`
    - ≥2 命中 → 不修改，返 `{"ok": false, "error": "old_str matches N times"}`
    LLM 收到 ≥2 命中错误后应扩写 old_str 缩小范围后重试。
    """

    old_str: str = Field(min_length=1, description="待替换的原文片段，必须精确匹配")
    new_str: str = Field(min_length=1, description="替换后的新文本")


class AuditSignalsPayload(BaseModel):
    """Redis `audit:{sid}` 单 key 三态信号管道值。

    - pending: 已入队等待 worker 处理
    - ready: worker 完成，signals 就绪
    - failed: 重试用尽，error 描述失败原因
    TTL 24h（config.audit_redis_ttl_seconds），到期自动过期。
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
