# M2 · 数据库 Schema 落地 — 实施计划 (2/17)

## 目标概述

将技术架构讨论中已确认的数据库 Schema 设计，落地为 SQLAlchemy ORM 模型 + Alembic 初始迁移脚本，覆盖 accounts / chat / audit / parent 四大模块共 12 张表，并部署关键索引策略。本计划不涉及业务逻辑实现（M3+ 范围）。

---

## 技术决策基线

| 决策项 | 结论 | 说明 |
| --- | --- | --- |
| Enum 策略 | PostgreSQL 原生 Enum | role / status 等有限枚举字段全部使用 PG Enum，DB 层类型安全；后续加值用 ALTER TYPE ... ADD VALUE |
| UUID 生成 | server_default=gen_random_uuid() | DB 层保底生成，避免应用层遗漏 |
| 时间戳 | 全部 TIMESTAMPTZ | 存储 UTC，展示时按用户时区转换；PostgreSQL 最佳实践 |
| messages.role | human / ai | 对齐 LangChain 消息类型（HumanMessage / AIMessage），DB 加载后直接映射 |
| 文件组织 | 按模块拆分 | models/[accounts.py](http://accounts.py)、[chat.py](http://chat.py)、[audit.py](http://audit.py)、[parent.py](http://parent.py)，清晰对应四大模块 |
| BaseMixin 字段 | 仅 id + created_at | 故意不含 updated_at；仅 rolling_summaries / device_tokens 等需要 upsert 语义的表显式声明带 onupdate 的 updated_at，避免 ORM 对只写入一次的表产生无谓更新 |
| 测试 | M2 不写 CRUD 测试 | SQLAlchemy 默认行为，后续用业务数据验证 |

---

## Enum 类型清单

以下 Enum 在模型定义中统一声明，Alembic 迁移自动创建：

| Enum 名称 | 值 | 使用位置 |
| --- | --- | --- |
| user_role | parent, child | users.role |
| sub_tier | free, paid | families.sub_tier |
| session_status | active, deleted | sessions.status |
| message_role | human, ai | messages.role |
| notification_type | crisis, redline, daily_summary | notifications.type |
| deletion_status | pending, completed, failed | data_deletion_requests.status |
| intervention_type | crisis, redline, guided | messages.intervention_type |
| gender | male, female, unknown | child_profiles.gender |
| device_platform | ios, android | device_tokens.platform |
| daily_status | stable, attention, alert | daily_reports.overall_status |

---

## 共享 Pydantic 类型定义

以下类型定义在 `app/schemas/` 目录下，被家长配置 API、审查 Agent structured output、日终专家 Agent 共同引用。

**`app/schemas/sensitivity.py`**：

```python
from pydantic import BaseModel, Field

class SensitivityConfig(BaseModel):
    """家长配置的 7 维度敏感度，每维度 0-9，默认 5。
    存储在 child_profiles.sensitivity JSONB 字段中。
    注入审查 Agent 提示词，影响各维度判断标准。
    """
    emotional: int = Field(default=5, ge=0, le=9, description="情绪与心理：压力、焦虑、孤独、自我否定")
    social: int = Field(default=5, ge=0, le=9, description="人际与社交：被排挞、霸凌、同伴压力、交友困惑")
    romance: int = Field(default=5, ge=0, le=9, description="恋爱与亲密关系：暗恋、表白、青春期性教育、身体变化")
    values: int = Field(default=5, ge=0, le=9, description="价值观与世界观：宗教、道德、社会争议话题")
    boundaries: int = Field(default=5, ge=0, le=9, description="AI 行为边界：角色扮演、模拟恋爱、过度亲密语气")
    academic: int = Field(default=5, ge=0, le=9, description="学习独立性：过度依赖 AI、直接要答案")
    lifestyle: int = Field(default=5, ge=0, le=9, description="生活方式：熬夜、游戏沉迷、饮食、烟酒、能量饮料")

class DimensionScore(BaseModel):
    """审查 Agent 对单个维度的评分结果。"""
    score: int = Field(ge=0, le=9, description="该维度的风险评分 0-9")
    detail: str = Field(default="", description="简要说明评分理由")

class AuditDimensionScores(BaseModel):
    """审查 Agent structured output 中的维度评分部分。
    与 SensitivityConfig 共享相同的 7 个维度 key。
    审查 Agent 在评分时已内化家长的 sensitivity 配置，
    输出的分数即为最终结果，无需事后叠加。
    """
    emotional: DimensionScore = Field(default_factory=lambda: DimensionScore(score=0))
    social: DimensionScore = Field(default_factory=lambda: DimensionScore(score=0))
    romance: DimensionScore = Field(default_factory=lambda: DimensionScore(score=0))
    values: DimensionScore = Field(default_factory=lambda: DimensionScore(score=0))
    boundaries: DimensionScore = Field(default_factory=lambda: DimensionScore(score=0))
    academic: DimensionScore = Field(default_factory=lambda: DimensionScore(score=0))
    lifestyle: DimensionScore = Field(default_factory=lambda: DimensionScore(score=0))
```

**设计说明**：

- `SensitivityConfig`：家长配置的输入类型，存储在 `child_profiles.sensitivity`，注入审查提示词
- `AuditDimensionScores`：审查 Agent 的输出类型，存储在 `audit_records` 中，与 `SensitivityConfig` 共享相同的 7 个 key
- 审查 Agent 在评分时已经通过提示词内化了家长的 sensitivity 配置，输出的分数即为最终结果，不需要事后叠加

**`app/schemas/rolling_summary.py`**：

```python
from pydantic import BaseModel, Field

class TurnSummaryEntry(BaseModel):
    """单轮对话的客观中立短摘要。
    审查图每轮 append 一条；供主对话图超滑动窗口后的上下文压缩。
    摘要口吻严格客观中立，禁带风控判断（避免主 LLM 返回视野后被污染）；
    紧张度信息已内化在摘要文字中（如「略沮丧」 vs「情绪崩溃」），无需额外数值字段。
    """
    turn: int = Field(ge=1, description="对话轮次编号，与 audit_records.turn_number 对应")
    summary: str = Field(description="该轮对话的客观中立短摘要，20-60 字；禁止风控口吻")
```

**设计说明**：

- `rolling_summaries.turn_summaries` 存 `list[TurnSummaryEntry]` 的 JSON 序列化结果；审查 Agent 每轮 structured output 包含 `turn_summary: str`（单行），`write_results` 节点 append 时附带 turn 编号
- 维度时序数据在日终专家需要时直接从 `audit_records.dimension_scores` 读原始 7 维度（比 turn 级 max 更精细）；turn_summaries 不冗存派生数值
- `rolling_summaries.session_notes` 是 TEXT 字段，由审查 Agent 按固定骨架整段重写维护，骨架包含四个章节：话题脉络 / 风险观察 / 情绪走向 / 家长关注点回应
- 消费方分工：
    - **session_notes**（风控视角笔记）：审查 Agent 自己跨轮复用 + 日终专家生成家长报告。**不注入主 LLM**，避免风控判断泄漏影响自然对话
    - **turn_summaries**（客观事实流水）：主对话图超窗压缩时注入主 LLM + 日终专家画风险时序曲线
- 设计取舍：LLM structured output 不再输出数组型 current_topics / current_flags，也不单独输出综合 risk_score；话题/风险/情绪统一收入 session_notes 骨架，综合分由代码从 7 维度派生

**`app/schemas/daily_report.py`**：

```python
from pydantic import BaseModel, Field

class DimensionDaily(BaseModel):
    """单维度当日汇总，由代码层从当日 audit_records.dimension_scores 聚合得出。"""
    peak: int = Field(ge=0, le=9, description="当日最大 score（最敏感那一刻）")
    mean: float = Field(ge=0.0, le=9.0, description="当日稳态水平（均分），给跨日对比留基线")
    high_turns: int = Field(ge=0, description="当日 score ≥ 7 的轮次数（突发频次）")

class DailyDimensionSummary(BaseModel):
    """当日 7 维度汇总。
    存储在 daily_reports.dimension_summary JSONB 字段中。
    代码层（非 LLM）聚合得出，作为日终专家 LLM 写报告时的量化锚点 + UI 雷达图数据源。
    """
    emotional: DimensionDaily
    social: DimensionDaily
    romance: DimensionDaily
    values: DimensionDaily
    boundaries: DimensionDaily
    academic: DimensionDaily
    lifestyle: DimensionDaily
```

**设计说明**：

- `peak` 回答“最敏感那一刻”、`mean` 回答“稳态水平”、`high_turns` 回答“突发频次”——三指标互补，避免 max 被偶发吓到 / mean 掩盖异常
- `mean` 在 MVP 阶段仅作数据留存，为后续跨日对比（“本周 emotional 均分高于上周”）留基线
- 日终专家 LLM 读取这份聚合 + session_notes + turn_summaries + child_profile 后，输出 `overall_status`（stable/attention/alert）+ markdown 报告
- 家长 UI：列表页用 overall_status 做色彩标识（绿/黄/红）；详情页读 content 显示报告；进阶用 DailyDimensionSummary 画雷达图

---

## 文件结构

```jsx
backend/app/schemas/
├── __init__.py
├── sensitivity.py        # SensitivityConfig / AuditDimensionScores（M2 定义）
├── rolling_summary.py    # TurnSummaryEntry（M2 定义）
├── daily_report.py       # DimensionDaily / DailyDimensionSummary（M2 定义）
├── audit.py              # 审查图 State + structured output（M8 定义）
├── chat.py               # 主对话图 State（M6 定义）
└── expert.py             # 日终专家图 State（M12 定义）

backend/app/models/
├── __init__.py        # 统一导出所有模型，Alembic metadata 自动发现
├── base.py            # DeclarativeBase + BaseMixin（id, created_at）
├── enums.py           # 所有 PostgreSQL Enum 定义
├── accounts.py        # families, users, child_profiles, auth_tokens, device_tokens
├── chat.py            # sessions, messages
├── audit.py           # audit_records, rolling_summaries
└── parent.py          # daily_reports, notifications, data_deletion_requests
```

---

## 执行步骤

### Step 1：ORM 基础设施

- [ ]  增强 `app/models/base.py`：添加 `BaseMixin`
- [ ]  创建 `app/models/enums.py`：定义所有 Enum 类型
- [ ]  更新 `app/models/__init__.py`：统一导出

**`app/models/base.py`**：

```python
import uuid
from datetime import datetime

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""
    pass

class BaseMixin:
    """公共字段混入：id + created_at。"""
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
```

**`app/models/enums.py`**：

```python
import enum

class UserRole(str, enum.Enum):
    parent = "parent"
    child = "child"

class SubTier(str, enum.Enum):
    free = "free"
    paid = "paid"

class SessionStatus(str, enum.Enum):
    active = "active"
    deleted = "deleted"

class MessageRole(str, enum.Enum):
    human = "human"
    ai = "ai"

class NotificationType(str, enum.Enum):
    crisis = "crisis"
    redline = "redline"
    daily_summary = "daily_summary"

class DeletionStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"

class InterventionType(str, enum.Enum):
    crisis = "crisis"      # 系统硬底线触发的三级接管
    redline = "redline"    # 家长红线触发的三级接管
    guided = "guided"      # 二级注入提醒后的回复

class Gender(str, enum.Enum):
    male = "male"
    female = "female"
    unknown = "unknown"

class DevicePlatform(str, enum.Enum):
    ios = "ios"
    android = "android"

class DailyStatus(str, enum.Enum):
    stable = "stable"          # 平稳：无明显风险信号
    attention = "attention"    # 关注：出现需留意的观察
    alert = "alert"            # 警示：触发危机/红线或连续高分维度
```

**`app/models/__init__.py`**：

```python
from app.models.base import Base
from app.models.accounts import Family, User, ChildProfile, AuthToken, DeviceToken
from app.models.chat import Session, Message
from app.models.audit import AuditRecord, RollingSummary
from app.models.parent import DailyReport, Notification, DataDeletionRequest

__all__ = [
    "Base",
    "Family", "User", "ChildProfile", "AuthToken", "DeviceToken",
    "Session", "Message",
    "AuditRecord", "RollingSummary",
    "DailyReport", "Notification", "DataDeletionRequest",
]
```

**验证**：import 各模块无报错

**提交**：`chore: add ORM base mixin and enum definitions`

---

### Step 2：accounts 模块建模（5 张表）

- [ ]  实现 `app/models/accounts.py`
- [ ]  包含 `families`、`users`、`child_profiles`、`auth_tokens`、`device_tokens`

**`app/models/accounts.py`**：

```python
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import ForeignKey, String, Boolean, Date, Text, text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseMixin
from app.models.enums import UserRole, SubTier, DevicePlatform, Gender

class Family(BaseMixin, Base):
    """家庭单元。注册父账号时自动创建，前期用户不感知。"""
    __tablename__ = "families"

    sub_tier: Mapped[SubTier] = mapped_column(
        default=SubTier.free,
        server_default="free",
        nullable=False,
    )

    # relationships
    users: Mapped[list["User"]] = relationship(back_populates="family")

class User(BaseMixin, Base):
    """用户账号，父/子共用一张表，通过 role 区分。"""
    __tablename__ = "users"

    family_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("families.id"), nullable=False,
    )
    role: Mapped[UserRole] = mapped_column(nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, comment="仅父账号",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False,
    )
    consent_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="监护人同意时间（仅 parent）",
    )
    consent_version: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, comment="同意的隐私政策版本",
    )

    # relationships
    family: Mapped["Family"] = relationship(back_populates="users")
    child_profile: Mapped[Optional["ChildProfile"]] = relationship(
        back_populates="child_user", uselist=False,
        foreign_keys="[ChildProfile.child_user_id]",
    )

class ChildProfile(BaseMixin, Base):
    """子账号画像配置，由家长创建和管理。"""
    __tablename__ = "child_profiles"

    child_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
        comment="创建者（审计用途；权限通过 family_id 控制）",
    )
    birth_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True,
        comment="家长输入 age，存近似生日 today - age years",
    )
    gender: Mapped[Optional[Gender]] = mapped_column(nullable=True)
    concerns: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="家长自然语言描述的关注点，注入审查提示词和日终专家提示词",
    )
    sensitivity: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="SensitivityConfig JSON，7 维度 0-9（默认 5）",
    )
    custom_redlines: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="家长自然语言描述的红线话题，审查 Agent 作为 0/1 判定条件，"
               "命中触发三级接管（温和转移）+ 通知家长",
    )

    # relationships
    child_user: Mapped["User"] = relationship(
        back_populates="child_profile",
        foreign_keys="[ChildProfile.child_user_id]",
    )

class AuthToken(BaseMixin, Base):
    """登录令牌。子账号 expires_at=NULL 表示永不过期。"""
    __tablename__ = "auth_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="NULL = 永不过期（子账号）",
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="父账号解绑时写入",
    )
    device_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

class DeviceToken(BaseMixin, Base):
    """设备推送令牌，用于向父账号设备发送通知。"""
    __tablename__ = "device_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    platform: Mapped[DevicePlatform] = mapped_column(nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

**设计说明**：

- `ChildProfile` 有两个 FK 指向 `users`：`child_user_id`（被配置的孩子）和 `created_by`（创建者家长），relationship 只建了 child_user 方向
- `DeviceToken.updated_at` 支持 token 刷新场景

**验证**：`from app.models.accounts import *` 无报错，模型关系正确

**提交**：`feat: add accounts module ORM models (5 tables)`

---

### Step 3：chat 模块建模（2 张表）

- [ ]  实现 `app/models/chat.py`
- [ ]  包含 `sessions`、`messages`

**`app/models/chat.py`**：

```python
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, String, Text, Index, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseMixin
from app.models.enums import SessionStatus, MessageRole, InterventionType

class Session(BaseMixin, Base):
    """对话会话。每个子账号可拥有多个会话。"""
    __tablename__ = "sessions"
    __table_args__ = (
        Index("idx_sessions_child", "child_user_id", "status"),
    )

    child_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    status: Mapped[SessionStatus] = mapped_column(
        default=SessionStatus.active,
        server_default="active",
        nullable=False,
    )
    last_active_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # relationships
    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", order_by="Message.created_at",
    )

class Message(BaseMixin, Base):
    """对话消息。role 使用 human/ai 对齐 LangChain 消息类型。"""
    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_messages_session", "session_id", "created_at"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intervention_type: Mapped[Optional[InterventionType]] = mapped_column(
        nullable=True,
        comment="null=正常回复, crisis=危机接管, redline=红线接管, guided=二级注入后回复",
    )

    # relationships
    session: Mapped["Session"] = relationship(back_populates="messages")
```

**验证**：模型导入无报错，索引定义正确

**提交**：`feat: add chat module ORM models (sessions, messages)`

---

### Step 4：audit 模块建模（2 张表）

- [ ]  实现 `app/models/audit.py`
- [ ]  包含 `audit_records`、`rolling_summaries`

**`app/models/audit.py`**：

```python
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, Float, Boolean, Text, text, func, Index
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin

class AuditRecord(BaseMixin, Base):
    """审查记录。每轮对话一条，保留原始打分。"""
    __tablename__ = "audit_records"
    __table_args__ = (
        Index("idx_audit_session", "session_id", "turn_number"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False,
    )
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    dimension_scores: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="AuditDimensionScores JSON：7 维度 score(0-9) + detail；"
               "供日终专家按维度诊断与跨日聚合；需要综合分时由代码派生 max(score)，不单独存储",
    )
    crisis_detected: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False,
    )
    crisis_topic: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    guidance_injection: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="注入的指引内容",
    )
    redline_triggered: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False,
        comment="家长红线命中 0/1",
    )
    redline_detail: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="命中的红线内容",
    )
    notify_sent: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False,
    )

class RollingSummary(BaseMixin, Base):
    """滚动摘要。每个 session 一条，每轮 upsert 更新。"""
    __tablename__ = "rolling_summaries"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"),
        unique=True, nullable=False,
    )
    last_turn: Mapped[int] = mapped_column(Integer, nullable=False)
    crisis_locked: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False,
        comment="crisis 粘性接管标志。一旦命中 crisis 置 true，该 session 剩余轮次全部由危机 LLM 接管；"
               "session 内不可逆，仅开启新 session 可重置。redline 不粘性，每轮重评估。",
    )
    session_notes: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="风控视角的跨轮叙事笔记（TEXT），审查 Agent 按固定骨架整段重写维护："
               "话题脉络 / 风险观察 / 情绪走向 / 家长关注点回应。"
               "供审查自身跨轮复用 + 日终专家生成家长报告；不注入主 LLM，避免风控判断泄漏",
    )
    turn_summaries: Mapped[Optional[list]] = mapped_column(
        JSONB, nullable=True,
        comment="list[TurnSummaryEntry] JSON：每轮客观中立短摘要（turn + summary）；"
               "供主对话图超窗压缩时注入主 LLM；"
               "日终专家时序分析直接读 audit_records.dimension_scores 原始数据，更精细",
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
```

**验证**：模型导入无报错，`rolling_summaries.session_id` UNIQUE 约束正确

**提交**：`feat: add audit module ORM models (audit_records, rolling_summaries)`

---

### Step 5：parent 模块建模（3 张表）

- [ ]  实现 `app/models/parent.py`
- [ ]  包含 `daily_reports`、`notifications`、`data_deletion_requests`

**`app/models/parent.py`**：

```python
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import ForeignKey, Date, Text, text, Index
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin
from app.models.enums import NotificationType, DeletionStatus, DailyStatus

class DailyReport(BaseMixin, Base):
    """日终报告。每孩子每天最多一条。"""
    __tablename__ = "daily_reports"
    __table_args__ = (
        Index("idx_reports_child", "child_user_id", "report_date"),
    )

    child_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    overall_status: Mapped[DailyStatus] = mapped_column(
        nullable=False,
        comment="LLM 综合判断的当日整体状态（stable/attention/alert），UI 列表页色彩标识依据",
    )
    dimension_summary: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="DailyDimensionSummary JSON：7 维度当日 peak / mean / high_turns；"
               "代码层从当日 audit_records.dimension_scores 聚合，供 LLM 量化锚点 + UI 雷达图 + 跨日对比",
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="markdown 格式报告",
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )

class Notification(BaseMixin, Base):
    """家长通知（危机实时推送 / 日常摘要）。"""
    __tablename__ = "notifications"

    parent_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    type: Mapped[NotificationType] = mapped_column(nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )

class DataDeletionRequest(BaseMixin, Base):
    """数据删除请求追踪，合规要求。"""
    __tablename__ = "data_deletion_requests"

    child_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
        comment="发起删除的家长",
    )
    status: Mapped[DeletionStatus] = mapped_column(
        default=DeletionStatus.pending,
        server_default="pending",
        nullable=False,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
```

**验证**：模型导入无报错，索引定义正确

**提交**：`feat: add parent module ORM models (daily_reports, notifications, data_deletion_requests)`

---

### Step 6：Alembic 初始迁移

- [ ]  确认 `alembic/env.py` 中 `target_metadata = Base.metadata` 指向更新后的 `__init__.py`
- [ ]  执行 `alembic revision --autogenerate -m "initial schema: 12 tables"`
- [ ]  审查生成的迁移脚本，确认：
    - 10 个 Enum 类型正确创建
    - 12 张表按依赖顺序创建（families → users → 其余）
    - 4 个复合索引已包含
    - 所有 FK 约束正确
- [ ]  执行 `alembic upgrade head`

**验证**：

- Docker Compose 启动 PostgreSQL → 迁移执行成功
- pgAdmin 确认 12 张表、10 个 Enum 类型、4 个复合索引全部存在
- `alembic current` 显示最新版本

**提交**：`feat: add initial Alembic migration (12 tables, 10 enums, 4 indexes)`

---

### Step 7：同步更新架构文档

- [ ]  更新 [技术架构讨论记录](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21) 中的 Schema 部分：
    - `messages.role` 从 `user / assistant` 改为 `human / ai`
    - 所有 TEXT 枚举字段标注为 PostgreSQL Enum
    - 时间戳字段标注 `TIMESTAMPTZ`
    - UUID 字段标注 `server_default gen_random_uuid()`

**提交**：`docs: update schema doc to reflect M2 decisions (enums, timestamptz, message role)`

---

## 索引策略总览

| 索引名 | 表 | 列 | 用途 |
| --- | --- | --- | --- |
| idx_messages_session | messages | (session_id, created_at) | 按会话加载消息历史，按时间排序 |
| idx_audit_session | audit_records | (session_id, turn_number) | 按会话查询审查记录，按轮次排序 |
| idx_sessions_child | sessions | (child_user_id, status) | 查询某个孩子的活跃/历史会话 |
| idx_reports_child | daily_reports | (child_user_id, report_date) | 按孩子 + 日期查询日终报告 |

---

## 完整表清单（12 张）

| 模块 | 表名 | 说明 |
| --- | --- | --- |
| accounts | families | 家庭单元 |
| accounts | users | 父/子账号 |
| accounts | child_profiles | 子账号画像配置 |
| accounts | auth_tokens | 登录令牌 |
| accounts | device_tokens | 设备推送令牌 |
| chat | sessions | 对话会话 |
| chat | messages | 对话消息 |
| audit | audit_records | 每轮审查记录 |
| audit | rolling_summaries | 滚动摘要（每 session 一条） |
| parent | daily_reports | 日终报告 |
| parent | notifications | 家长通知 |
| parent | data_deletion_requests | 数据删除请求追踪 |