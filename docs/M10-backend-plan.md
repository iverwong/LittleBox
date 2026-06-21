# M10 · 家长配置 — 实施计划 (10/17)

<aside>
🎯

**里程碑**：M10 家长配置（后端）。**分支**：`feat/m10-parent-config`。

**核心**：把家长配置从「年龄 / 性别 / 生日」补全到「基础 + 关注点 concerns + 高级配置 sensitivity / custom_redlines」，新增独立 `child_profile` 资源（GET / PATCH），并把 concerns 接入安全审查 prompt。

</aside>

## 目标概述

**做什么**

- 新增 `SensitivityConfig`（6 维、1–9、默认 5）作为关注度配置的单一事实源（前端 / API / 审查 prompt 共用 key）
- 新增父端 `child_profile` 资源：`GET / PATCH /api/v1/child-profiles/{child_user_id}`，部分更新全部配置字段
- `ChildProfileSnapshot` 补 `concerns`，注入安全审查 prompt（仅 audit）
- 抽查询 helper（`load_child_profile` / `load_child_profile_in_family`）+ snapshot builder，收口三处 `select(ChildProfile)`

**不做什么（Non-goals）**

- 前端界面 / 配置引导流程（留前端里程碑）
- 订阅层级 / tier gating（产品阶段一付费功能全免费试用，后续里程碑再做）
- 日终教育专家消费 concerns（M12）
- 改动子端 `/me/profile` 与 `ChildProfileOut`（已记后续待办，前端改造时一并摘除）
- Alembic 迁移（`concerns` / `sensitivity` / `custom_redlines` 三列已存在）
- 改动审查打分轴 `AuditDimensionScores`（0–9）与其 6 维口径

## 关键契约抄录（自包含）

### 6 个风险维度 + 关注度语义

keys（前端 / API / 审查 prompt 共用）：`emotional 情绪与心理` / `social 人际与社交` / `values 价值观与世界观` / `boundaries AI 应用边界` / `academic 学习独立性` / `lifestyle 生活方式`。

关注度数值对齐 `audit/prompts.py` 的 `LEVEL_MAP`（**1–9**，默认 **5**）：

1 完全不关注 · 2 几乎不关注 · 3 较少关注 · 4 略偏宽松 · 5 正常关注 · 6 略加留意 · 7 较为关注 · 8 高度关注 · 9 极度关注。配置越高 → 该维度家长越关注、审查越严。

<aside>
⚠️

**两套轴别混**：家长**关注度配置** = 1–9（本里程碑的 `SensitivityConfig`，对齐 `LEVEL_MAP`）；审查**打分** `AuditDimensionScores` = 0–9（0 = 本轮无相关内容）。本里程碑只动配置轴，不动打分轴。

</aside>

### 配置生效时机（技术架构 §13.10）

`child_profile` 是运行期数据、每轮现读（chat_stream 每请求 `SELECT`、审查每 run 重建 snapshot），**无 Redis 缓存**。改配置**无需失效任何缓存**，下一轮自然生效；已开跑的当前轮用入口已捕获的旧 snapshot，不受影响。因此本里程碑的更新链路**零 Redis 副作用**。

## 前置条件

- [ ]  main 最新，工作区干净
- [ ]  Python 3.14 容器可用（`docker compose exec api python --version`）
- [ ]  `docker compose` 起 api / postgres / redis
- [ ]  确认 `child_profiles` 表已有 `concerns`(Text, nullable) / `sensitivity`(JSONB, nullable) / `custom_redlines`(Text, nullable) 三列 → 本里程碑**不应**产生 Alembic 迁移

<aside>
🐍

**注释 / docstring 风格 = Google（与现有代码一致，本里程碑新增代码一律照此）**：函数 / 方法 docstring 须含「摘要行」+ 视情况补 `Args:` / `Returns:` / `Raises:` 段；模型 / 类用摘要行（字段语义走 `Field(description=...)`）。行内注释解释「为什么」而非复述代码。可参照现有 `accounts/service.py` 的 `age_to_birth_date` / `create_child` / `hard_delete_child`。下方贴出的 docstring 均已按此风格写好，**直接照抄**。

</aside>

## 执行步骤

### Step 1 · 建分支 + Schema 层

**任务**

- [ ]  `git checkout -b feat/m10-parent-config`
- [ ]  `accounts/schemas.py` 新增 `SensitivityConfig`
- [ ]  新增 `UpdateChildProfileRequest`（PATCH 部分更新）
- [ ]  新增 `ChildProfileDetail`（父端读回，全字段）
- [ ]  `ChildProfileSnapshot` 增加 `concerns: str | None`

**新增契约**

```python
class SensitivityConfig(BaseModel):
    """家长对 6 个风险维度的关注度配置（1–9，默认 5）。

    数值语义对齐 audit/prompts.py 的 LEVEL_MAP（1=完全不关注 … 5=正常关注 … 9=极度关注）：
    配置越高 = 该维度家长越关注、审查越严；越低 = 越宽容。
    作为前端 / API / 审查 prompt 共用 6 个 key 的单一事实源。
    """

    emotional: int = Field(5, ge=1, le=9, description="情绪与心理")
    social: int = Field(5, ge=1, le=9, description="人际与社交")
    values: int = Field(5, ge=1, le=9, description="价值观与世界观")
    boundaries: int = Field(5, ge=1, le=9, description="AI 应用边界")
    academic: int = Field(5, ge=1, le=9, description="学习独立性")
    lifestyle: int = Field(5, ge=1, le=9, description="生活方式")
```

```python
class UpdateChildProfileRequest(BaseModel):
    """PATCH /api/v1/child-profiles/{child_user_id} 请求体（部分更新）。

    全字段 Optional：未传 = 不动。
    可空字段（concerns / custom_redlines / sensitivity）传 null = 清空；
    非空字段（nickname / age / gender）传 null 视为不动（DB NOT NULL，语义不允许清空）。
    sensitivity 为整体替换（提交完整 6 维），不做维度级 merge。
    """

    nickname: str | None = Field(None, min_length=1, max_length=32)
    age: int | None = Field(None, ge=3, le=21)
    gender: Literal["male", "female", "unknown"] | None = None
    concerns: str | None = None
    sensitivity: SensitivityConfig | None = None
    custom_redlines: str | None = None
```

```python
class ChildProfileDetail(BaseModel):
    """GET / PATCH /api/v1/child-profiles/{child_user_id} 响应体（父端全字段）。"""

    child_user_id: uuid.UUID
    nickname: str
    gender: Literal["male", "female", "unknown"]
    birth_date: date
    age: int
    concerns: str | None
    sensitivity: SensitivityConfig | None
    custom_redlines: str | None
```

`ChildProfileSnapshot` 仅加一字段 `concerns: str | None`（紧随 custom_redlines 字段映射）。

**验证清单**

- [ ]  basedpyright 通过（新模型无类型错误）
- [ ]  ruff 通过
- [ ]  import 无环（schemas 不反向引 models）

**commit**：`feat: add child profile config schemas (sensitivity/update/detail + snapshot concerns)`

### Step 2 · 查询复用 + service 更新逻辑

**任务**

- [ ]  `accounts/service.py` 新增 `load_child_profile` / `load_child_profile_in_family`
- [ ]  新增 `build_child_profile_snapshot(profile)`（收口 `age_at` 换算 + 字段映射，含 concerns）
- [ ]  新增 `update_child_profile(...)`（部分更新 + family 归属 + `commit_with_redis`）
- [ ]  `me.py` chat_stream 改用 `load_child_profile` + `build_child_profile_snapshot` 收口（行为不变，新增 concerns）

**新增实现**

```python
from app.core.time import age_at  # service.py 顶部补这一 import

async def load_child_profile(db: AsyncSession, child_user_id: uuid.UUID) -> ChildProfile | None:
    """按 child_user_id 加载 profile（自身 / LLM 路径用，无 family 约束）。

    Args:
        db: 数据库会话。
        child_user_id: 子账号 `User.id`。

    Returns:
        命中的 `ChildProfile`；不存在返回 `None`。
    """
    return (
        await db.execute(select(ChildProfile).where(ChildProfile.child_user_id == child_user_id))
    ).scalar_one_or_none()

async def load_child_profile_in_family(
    db: AsyncSession, *, child_user_id: uuid.UUID, family_id: uuid.UUID
) -> ChildProfile | None:
    """父端访问 child profile 的唯一入口，family 归属焊进同一条 WHERE（防 IDOR）。

    Args:
        db: 数据库会话。
        child_user_id: 目标子账号 `User.id`。
        family_id: 当前父账号所属 family，作为 WHERE 约束的一部分。

    Returns:
        本 family 内命中的 `ChildProfile`；不存在或越权返回 `None`。
    """
    return (
        await db.execute(
            select(ChildProfile)
            .join(User, User.id == ChildProfile.child_user_id)
            .where(
                ChildProfile.child_user_id == child_user_id,
                User.family_id == family_id,
                User.role == UserRole.child,
                User.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()

def build_child_profile_snapshot(profile: ChildProfile) -> ChildProfileSnapshot:
    """构造 snapshot，收口 age_at 换算与字段映射（含 concerns）。

    Args:
        profile: 已加载的 `ChildProfile` ORM 对象。

    Returns:
        填好全部字段的 `ChildProfileSnapshot`（frozen）。
    """
    return ChildProfileSnapshot(
        child_user_id=profile.child_user_id,
        nickname=profile.nickname,
        gender=profile.gender.value,
        birth_date=profile.birth_date,
        age=age_at(profile.birth_date, tz="Asia/Shanghai"),
        sensitivity=profile.sensitivity,
        custom_redlines=profile.custom_redlines,
        concerns=profile.concerns,
    )

async def update_child_profile(
    db: AsyncSession,
    redis: Redis,
    *,
    parent: CurrentAccount,
    child_user_id: uuid.UUID,
    payload: UpdateChildProfileRequest,
) -> ChildProfile:
    """父端部分更新子账号配置。

    PATCH 语义：仅 `exclude_unset` 的字段参与更新；非空字段（nickname / age /
    gender）传 null 视为不动，可空字段（concerns / custom_redlines / sensitivity）
    传 null 即清空；sensitivity 为整体替换。family 归属在
    `load_child_profile_in_family` 内焊入 WHERE。

    Args:
        db: 数据库会话。
        redis: Redis 客户端，随 commit 一并 flush staged ops。
        parent: 当前父账号上下文，提供 `family_id`。
        child_user_id: 目标子账号 `User.id`。
        payload: 部分更新请求体。

    Returns:
        更新后的 `ChildProfile`。

    Raises:
        HTTPException: child 不存在或非本 family 时抛 404。
    """
    profile = await load_child_profile_in_family(
        db, child_user_id=child_user_id, family_id=parent.family_id
    )
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "child not found in family")

    data = payload.model_dump(exclude_unset=True)  # PATCH：仅动传入字段
    # 非空字段：传 null 视为不动
    if data.get("nickname") is not None:
        profile.nickname = data["nickname"]
    if data.get("gender") is not None:
        profile.gender = Gender(data["gender"])
    if data.get("age") is not None:
        profile.birth_date = age_to_birth_date(data["age"])  # 重算出生日期
    # 可空字段：传 null 即清空
    if "concerns" in data:
        profile.concerns = data["concerns"]
    if "custom_redlines" in data:
        profile.custom_redlines = data["custom_redlines"]
    if "sensitivity" in data:
        profile.sensitivity = data["sensitivity"]  # SensitivityConfig 已校验，dict|None 直落 JSONB

    await commit_with_redis(db, redis)  # 无 staged Redis op → 等价干净 DB commit，守 service「不裸 commit」纪律
    return profile
```

<aside>
📝

`me.py` chat_stream 内现有的「`select(ChildProfile)` + 手搓 `ChildProfileSnapshot(...)`」两段，替换为 `load_child_profile` + `build_child_profile_snapshot`；保留 `ChildProfileNotFound` 的 404 分支。纯收口，行为不变（额外多带 concerns）。

</aside>

**验证清单**

- [ ]  [me.py](http://me.py) chat_stream snapshot 字段与改前一致 + 多 concerns
- [ ]  basedpyright / ruff 通过

**commit**：`feat: add child profile query helpers and update_child_profile service`

### Step 3 · child_profile API 资源

**任务**

- [ ]  新增 `api/child_profiles.py`，`router = APIRouter(prefix="/api/v1/child-profiles", tags=["child-profiles"])`
- [ ]  `GET /{child_user_id}`：`require_parent` → `load_child_profile_in_family` → 404 → 组 `ChildProfileDetail`
- [ ]  `PATCH /{child_user_id}`：`require_parent` → `update_child_profile` → 组 `ChildProfileDetail`
- [ ]  在 app 注册 router 处（与 children / me 并列）`include_router(child_profiles.router)`

**契约**

- 错误码矩阵：401 未登录 / 403 非 parent（`require_parent` 抛）/ 404 child 不存在或非本 family（不暴露存在性）/ 200 成功
- `ChildProfileDetail` 组装：`age` 用 `age_at(birth_date, "Asia/Shanghai")`；`sensitivity` 读回用 `SensitivityConfig(**profile.sensitivity) if profile.sensitivity else None` 规整（extra key 忽略、缺省维度补 5）

```python
def _to_detail(profile: ChildProfile) -> ChildProfileDetail:
    """把 ChildProfile ORM 组装为父端响应体。

    Args:
        profile: 已加载的 `ChildProfile`。

    Returns:
        填好 age 换算与 sensitivity 规整的 `ChildProfileDetail`。
    """
    return ChildProfileDetail(
        child_user_id=profile.child_user_id,
        nickname=profile.nickname,
        gender=profile.gender.value,
        birth_date=profile.birth_date,
        age=age_at(profile.birth_date, tz="Asia/Shanghai"),
        concerns=profile.concerns,
        sensitivity=SensitivityConfig(**profile.sensitivity) if profile.sensitivity else None,
        custom_redlines=profile.custom_redlines,
    )
```

**验证清单**

- [ ]  `/docs` 出现 child-profiles 的 GET / PATCH
- [ ]  curl 冒烟：parent token GET / PATCH 正常；child token → 403；跨 family id → 404
- [ ]  ruff / basedpyright 通过

**commit**：`feat: add parent child-profile resource (GET/PATCH /api/v1/child-profiles/{id})`

### Step 4 · concerns 注入审查 prompt

**任务**

- [ ]  `audit/prompts.py` 增加 `concerns_section`（仅当 `child_profile.concerns` 非空注入，仿 `redline_section`）
- [ ]  在 prompt content f-string 中插入 `{concerns_section}`（建议置于「关于用户」段之后、「红线 / 六维度」之前）
- [ ]  更新 `build_audit_system_prompt` docstring 增提 concerns

**新增片段**

```python
concerns_section = ""
if child_profile.concerns:
    concerns_section = f"""
# 家长关注点(concerns)
家长额外标注了孩子近况 / 关注点,请在相关话题上提高敏感度、优先观察其走向:
{child_profile.concerns}
"""
```

<aside>
🔕

concerns **只进审查 prompt**，不进主对话 prompt——避免主 AI 知晓家长私域描述后不自然地主动提起、让孩子察觉被监督。

</aside>

**验证清单**

- [ ]  设 concerns → 渲染字符串含关注点正文；未设 → 不含且无空段
- [ ]  主对话 `build_system_prompt` 未引入 concerns
- [ ]  ruff / basedpyright 通过

**commit**：`feat: inject parent concerns into audit system prompt`

### Step 5 · 测试

**任务**

- [ ]  `tests/domain/accounts/test_sensitivity_config.py`：边界 1 / 9 合法、拒 0 / 10、默认 5
- [ ]  `tests/domain/accounts/test_child_profile_service.py`：部分更新只动传入字段、age 重算 birth_date、null 清空 concerns、sensitivity 整体替换、跨 family → 404
- [ ]  `tests/api/test_child_profiles.py`：GET / PATCH happy path、child token → 403、跨 family → 404
- [ ]  `tests/domain/audit/test_prompts_concerns.py`：concerns 注入 / 缺省两态渲染断言
- [ ]  测试函数级 docstring 用 Given / When / Then

<aside>
🔒

**测试隔离铁律**：所有涉及 DB / Redis 的测试**必须**经 `conftest.py` 的 `api_client` / `db_session` / fakeredis / `dependency_overrides` 进入。**禁止**：连真实库、httpx 直连真 server、`redis.Redis(...)` 显连、`from app.config import settings` 自建 engine、`flushdb()` / `flushall()`。

</aside>

**验证清单**

- [ ]  `docker compose exec api pytest tests/domain/accounts tests/api/test_child_profiles.py tests/domain/audit/test_prompts_concerns.py -q` 全绿
- [ ]  全量 `docker compose exec api pytest -q` 无回归

**commit**：`test: cover child profile config, resource, and concerns injection`

## 验收清单

- [ ]  ruff + basedpyright 全绿
- [ ]  新增 / 改动测试全绿，全量回归无破
- [ ]  `GET / PATCH /api/v1/child-profiles/{id}` 冒烟通过（parent 正常 / child 403 / 跨 family 404）
- [ ]  `alembic revision --autogenerate` 为空 diff（确认无需迁移）
- [ ]  子端 `/me/profile` 行为不变
- [ ]  设 concerns 后审查 prompt 含关注点、主对话 prompt 不含
- [ ]  收尾走 PR 合 main（合并前 `git log main..feat/m10-parent-config` 已全部纳入）

## 发现与建议

- 子端 `/me/profile` + `ChildProfileOut` 的摘除已记入后续待办：[移除子端 /me/profile 调用与端点（前端改造时）](https://app.notion.com/p/me-profile-edee273ea22e47508e55d7e6c3329311?pvs=21)
- `sensitivity` 在 DB 为 raw dict、无历史校验；读回经 `SensitivityConfig` 规整（extra key 忽略、缺省维度补 5），写入由 schema 层校验，达成「整体存入 db 保持统一」
- `LEVEL_MAP` 1–9 与 `AuditDimensionScores` 0–9 是两套轴（配置关注度 vs 审查打分），本里程碑只动配置轴
- tier gating 与日终专家消费 concerns 分别推迟至订阅里程碑 / M12