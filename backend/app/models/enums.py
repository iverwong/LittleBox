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
