"""全栈 enum 集中处。

DB 字段、Pydantic schema、业务分支统一从这里取枚举值，避免散落字符串。
"""

import enum


class UserRole(str, enum.Enum):
    """用户角色。"""

    parent = "parent"  # 父端管理员
    child = "child"  # 孩子端用户


class SubTier(str, enum.Enum):
    """订阅层级。"""

    free = "free"  # 免费版
    paid = "paid"  # 付费版


class SessionStatus(str, enum.Enum):
    """对话 session 状态。"""

    active = "active"  # 正常使用中
    deleted = "deleted"  # 已删除


class MessageStatus(str, enum.Enum):
    """单条 message 状态。"""

    active = "active"  # 正常展示
    discarded = "discarded"  # 已丢弃(不进 history、不进审查)
    compressed = "compressed"  # 已被上下文压缩吸收


class MessageRole(str, enum.Enum):
    """消息角色(对齐 LangChain HumanMessage / AIMessage)。"""

    human = "human"  # 用户
    ai = "ai"  # AI
    summary = "summary"  # 压缩产出的摘要


class NotificationType(str, enum.Enum):
    """通知类型。"""

    crisis = "crisis"  # 危机干预告警
    daily_summary = "daily_summary"  # 日终专家日报


class InterventionType(str, enum.Enum):
    """AI 介入等级。"""

    crisis = "crisis"  # 系统硬底线触发的三级接管
    guided = "guided"  # 二级注入提醒后的回复


class Gender(str, enum.Enum):
    """孩子性别。"""

    male = "male"  # 男
    female = "female"  # 女
    unknown = "unknown"  # 未知(创建时未填)


class DevicePlatform(str, enum.Enum):
    """设备平台。"""

    ios = "ios"  # iOS
    android = "android"  # Android


class DailyStatus(str, enum.Enum):
    """日报整体状态。"""

    stable = "stable"  # 平稳:无明显风险信号
    attention = "attention"  # 关注:出现需留意的观察
    alert = "alert"  # 警示:触发危机/红线或连续高分维度
