# M5 · 账号体系 - 前端 — 实施计划 (5/17)

## 目标概述

把 M4 + M4.8 后端账号能力联到前端：父端登录 / 建孩子 / 出码，子端扫码 / welcome，硬删闭环。M4.8 绑定凭证端点统一由 `/api/v1/bind-tokens/*` 提供。账号体系 M5 + M4.8 后视为基本封板。

**不做**：M7 聊天（仅 welcome 空壳） / parent 自助注册 / PNVS / token 自动刷新 / 暗色主题 / 页面视觉迭代 / 文档回写（NoNo 统一维护，不进执行 Step）。

## 全局交互约定（M5 所有 Step 默认遵循）

所有交互细节统一定义在此，F1-F7 各 Step 不再重复说明；其中 F3-F6 等涉及用户交互的 Step，验证清单末尾必须包含一条「全局交互约定已落实」做兜底检查。

### Loading 态

- **按钮触发的 API**：用 M4.5 Button 的 `loading` prop（内部已含 disabled）；执行时显式双写 `loading={mutation.isPending} disabled={mutation.isPending}`，避免依赖隐式行为
- **页面初次加载**：Skeleton 占位（如 F4 列表的 3 张 skeleton），不全屏 spinner
- **决策型 modal**（删除 confirm / 下线 confirm）「确认」按钮**不带 loading**：瞬时关 modal + 把 mutation 委托给列表按钮承载 loading
- **展示型 modal**（BindQrModal）内部按钮（刷新、复制）保留独立 loading
- **列表按钮 loading vs disabled 拆分**：短场景（单 API 调用）loading + disabled 同步生灭；长场景（modal 打开期间）只 disabled 不 loading；BindQrModal 触发时序：点击 → POST /bind-tokens 期间 loading + disabled → modal 打开后只 disabled → modal 关闭后释放
- **轮询期间**（BindQrModal status）：不显式 loading；QR 即视觉反馈，刷新瞬间 200ms 淡出淡入

### Toast 文案 / 时长

| 场景 | 类型 | duration | 文案 |
| --- | --- | --- | --- |
| 创建孩子成功 | success | 1.5s | 已添加 |
| 删除成功 | success | 1.5s | 已删除 |
| 绑定成功 | success | 1.5s | 已绑定 |
| 下线成功 | success | 1.5s | 已下线 |
| 出码失败（POST /bind-tokens） | error | 3s | 出码失败，请重试 |
| 下线失败 | error | 3s | 下线失败，请稍后重试 |
| 通用 5xx | error | 3s | 网络异常，稍后重试 |

**原则**：成功用 1.5s 短 toast（轻量确认，不打扰）；失败用 3s 标准 toast（带原因）。

### 错误反馈

- **网络异常 / 5xx**：Toast「网络异常，稍后重试」+ 不切页面
- **业务 4xx**：按 Step 既有兜底（401/403/409/422）；Toast 文案对齐 Step 内描述
- **API client 401 全局拦截**：直接 clearSession + 跳 landing（F1 已定义），不再弹 Toast（避免叠加）

### 防重入

- 所有 mutation API（POST/PATCH/DELETE）必须依赖 Button.loading 防重复点击
- Modal 内 confirm 按钮在请求未结束前禁止 onClose（含点遮罩）—— 但因决策型 modal「确认」按钮瞬时关后才发请求，此条主要约束展示型 modal

### 状态对齐（modal 写入型操作）

所有触发后端写操作的 modal 关闭后，UI 反馈以**最新真实状态**为准（而非 modal 内部假设的状态）。三种来源：

- **mutation 成功状态本身即权威**（如下线 POST /revoke-tokens 返回 204，无响应体）→ 按当前操作对象本地更新 state，**不 refetch**
- **modal 内部已知权威状态**（如 BindQrModal 轮询命中 bound）→ 信任内部信号，本地切 state + toast，**不 refetch**
- **关闭原因不确定**（用户主动关 / token 过期）→ refetch 兜底 + 对比前后状态差异决定 toast 文案

具体到 BindQrModal：onClose 必须传 `reason: 'bound' | 'user_close' | 'expired'`，父组件按 reason 分支处理。

## 前置条件 / 上游依赖

- M1 / M2 / M3 / M4 / M4-patch / M4.5 / M4.6 ✅
- **硬依赖**：[M4.8 · 账号后端补齐 — 实施计划 (4.8/17)](https://www.notion.so/M4-8-4-8-17-1c918b7e107d482591d4a73f6c58909c?pvs=21) 全部 B Step 合并主线（bind-tokens 独立资源路由 / nickname / age 转换 / POST·GET·DELETE /children / GET /me/profile / 级联硬删 + 审计 / alembic baseline 重建）
- 架构决策、Schema、级联范围、三级接管语义见 [技术架构讨论记录（持续更新）](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21)
- HTML 原型 v1.2 见 [M4.5 · 视觉基调 HTML 原型](https://www.notion.so/m45-html-prototype)；AgePicker 依赖 M4.6 DiscreteSlider

## 执行步骤

### Step F0-gate：前置确认

`depends_on: none`

**审批 + 分支准备 Step**，仅由 Iver 审批后放行后续编码：

- [ ]  `git checkout main && git pull && git checkout -b feat/m5-account-frontend`（F0 放行后、进入 F1 前执行；M5 后续提交全部落在该分支）
- [ ]  M4.5 收口（页面清单 + 工作流回写两项已判过度设计、不阻塞）
- [ ]  M4.8 全部 B Step 完成（含 alembic baseline）
- [ ]  HTML 原型 v1.2 视觉规格与 M4.6 DiscreteSlider 已落地

放行后才能进入 F0.5。

---

### Step B0-quota：后端 family 3 子账号上限补丁

`depends_on: [F0-gate]`，blocks: F4

**决策来源**：本次 M5 评审（2026-04-27）拍板。原 M4.8 未做 family 子账号数量上限，前端兜底（disabled 按钮 + Toast）只能挡单家长场景；M11+ 多家长共享 family 时两端并发建孩子能绕过。server-side 是唯一靠得住的拦截层。改动极小（~5 行代码 + 1 个测试），不破坏 M4.8 schema/路径，只新增 409 响应码，向前兼容。

- [ ]  `backend/app/api/children.py` POST handler 在 nickname 等校验之后、insert 之前插入 quota 检查：
    - `count = db.scalar(select(func.count(Child.id)).where(Child.family_id == family_id))`
    - `if count >= CHILD_QUOTA_PER_FAMILY: raise HTTPException(409, "child quota exceeded")`
- [ ]  模块顶部声明常量：`CHILD_QUOTA_PER_FAMILY = 3`，避免散落 magic number
- [ ]  后端单测：`tests/api/test_children.py` 加 `test_create_child_quota_exceeded`：建 3 个孩子后第 4 个 → 409 + body `{"detail": "child quota exceeded"}`
- [ ]  alembic 不动（无 schema 变更）；本 Step 是 M5 前置后端 hotfix，不触碰 M4.8 baseline，后续若有 schema 变化仍必须走独立 revision
- [ ]  文档：M4.8 plan / 偏差记录同步标注「已补 quota」（NoNo 维护，不进 commit）

**验证**：

- ✅ `uv run pytest tests/api/test_children.py -k quota` 通过
- ✅ `uv run pytest` 全绿
- ❌ 不允许在前端兜底基础上让 server 静默通过

**提交**：`feat(backend): family child quota=3 with 409 response (M5 B0-quota)`

---

### Step F0.5：路由命名空间规范化（group → 真目录）

`depends_on: [F0-gate]`

**决策来源**：本次 M5 评审（2026-04-27）拍板。原 `(auth)` / `(parent)` / `(child)` / `(dev)` group 经核查为「沿用 expo-router 默认未论证」选型；group URL 透明性与 M4.8 后端 router prefix「身份/资源在路径上分层」哲学相反，且本项目无「同 URL 多 layout」需求。趁 M5 落页面前一次性规范，避免 M6+ 路由爆增后改造成本上升。决策回写到 [技术架构讨论记录（持续更新）](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21) §七前端「路由命名空间决策」。

**目标 URL 命名空间对照**：

```
旧 (group, URL 透明)              新 (真目录, URL 显性)
/                                 /                          (bootstrap 决策器，保留)
/(auth)/landing                   /auth/landing
/(auth)/login                     /auth/login
/(auth)/bind/scan                 /auth/bind/scan
/(auth)/bind/manual               /auth/bind/manual
/(parent)/children                /parent/children
/(parent)/children/new            /parent/children/new
/(parent)/children/[id]           /parent/children/[id]
/(child)/welcome                  /child/welcome
/(dev)/components                 /dev/components
/dev-chat                         /dev/chat
(M5 F2 新增)                      /dev/hub
```

- [ ]  `git mv 'mobile/app/(auth)' mobile/app/auth`（其余 `(parent)` / `(child)` / `(dev)` 同理；单引号兜底括号在 shell 的特殊性，保留 git 历史）
- [ ]  `git mv mobile/app/dev-chat.tsx mobile/app/dev/chat.tsx`，组件 default export 改名为 `DevChat`，删 M3 临时 kebab-case 命名
- [ ]  `mobile/app/_layout.tsx` 清理 M3 残留：删 `unstable_settings.initialRouteName: 'dev-chat'`；解封被注释的 role guard 骨架（`useSegments` / `useRouter` / `useAuthStore` import + `RootLayoutNav` 内 useEffect 块）；解封被注释的 `<Stack.Screen name="(dev)" />` 占位并改名为 `name="dev"`，同步补 `name="auth"` / `name="parent"` / `name="child"`
- [ ]  `mobile/app/index.tsx` 改写为 bootstrap 决策器骨架（取代当前 M3-TEMP redirect 占位）：等 `useAuthStore.hydrated` 为 true → `__DEV__ && START_AT_DEV_HUB` 优先 → 否则按 role 分发到 `/auth/landing` / `/parent/children` / `/child/welcome`；具体 Dev Hub 决策与 5 按钮逻辑由 F2 接续完成
- [ ]  全仓 grep 清理路径字符串：所有 `router.push` / `<Redirect>` / `<Link>` / 字符串字面量按上方对照表统一改写
- [ ]  重新生成 typed routes：删 `mobile/.expo/types/router.d.ts` 让 expo-router 重生；`npm run typecheck` 触发；核对路由字面量集合仅包含 `/auth/*` `/parent/*` `/child/*` `/dev/*` 命名空间

**验证**：

- ✅ `npm run lint` + `npm run typecheck` 通过
- ✅ `mobile/.expo/types/router.d.ts` grep 不到 `(auth)` / `(parent)` / `(child)` / `(dev)` / `dev-chat`
- ✅ Dev 启动 → `/dev/components` 与 `/dev/chat` 均可访问；`useSegments()` 返回值无括号段
- ❌ 不允许残留任何 group 目录；不允许 `mobile/app/dev-chat.tsx` 残留

**提交**：`refactor(mobile): replace expo-router groups with explicit namespaces (M5 F0.5)`

---

### Step F1：API client + device_id + authStore

`depends_on: [F0-gate]`

- [ ]  `mobile/services/api/client.ts`：fetch 封装；自动注入 `Authorization: Bearer {token}` + `X-Device-Id: {deviceId}`；401 → `auth.clearSession()` + 跳 `auth/landing`；429 / 5xx → Toast
- [ ]  `mobile/hooks/useDeviceId.ts`：SecureStore 无 `auth.deviceId` → `Crypto.randomUUID()` 写入；后续读
- [ ]  沿用已存在的 `mobile/stores/auth.ts`，改造成 zustand auth 分片 `{ role, token, userId, deviceId, hydrated, hydrate, setSession, clearSession, resetDevice }`；敏感字段不 persist，由 SecureStore 水合；禁止新建并行 `authStore.ts`
- [ ]  SecureStore keys：`auth.token` / `auth.role` / `auth.userId` / `auth.deviceId`

**关键决策**：不缓存 nickname；welcome 页冷启动走 `GET /me/profile` 拉取（语义分离原则，详见 [技术架构讨论记录（持续更新）](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21) §六）

**验证**：

- ✅ 冷启首次 → SecureStore 出现 `auth.deviceId`
- ✅ 模拟 401 → 清会话 + 跳 landing
- ❌ 不允许 token / role / userId 只存在内存、冷启动后丢会话

**提交**：`feat(mobile): api client + device_id + authStore (M5 F1)`

---

### Step F2：Bootstrap + role guard + Dev Hub

`depends_on: [F0.5, F1]`

**Dev Hub 入口控制（Iver 2026-04-25 拍板）**：源码常量 `START_AT_DEV_HUB`，M15 部署前删 `mobile/app/dev/` 目录。理由：env / app.config.ts extra 引入额外配置层，对独立开发工作流增益小；源码常量改一行 + Reload 即可切换。

**与 F0.5 的衔接**：F0.5 已完成 group → 真目录改造、`mobile/app/index.tsx` bootstrap 决策器骨架与 `_layout.tsx` role guard 解封。F2 在此基础上：补 Dev Hub 页面与 5 按钮逻辑、把 `START_AT_DEV_HUB` 常量声明落到使用它的 `mobile/app/index.tsx`、把 role guard 的 hydrated 过渡态处理补齐。

- [ ]  `mobile/app/index.tsx` 顶部声明常量：`const START_AT_DEV_HUB = true as const`（常量声明在使用处；M15 清理时与 Dev Hub 分支一并删除）
- [ ]  `mobile/app/index.tsx` 决策器（接 F0.5 骨架补齐分支）：
    - 等 `useAuthStore.hydrated === true` 再做决策；hydrating 期间 `return null`（避免 SecureStore 未就绪时误跳）
    - `__DEV__ && START_AT_DEV_HUB` → `<Redirect href="/dev/hub" />`（`__DEV__` 托底防生产泄露）
    - 否则按 role：null → `<Redirect href="/auth/landing" />`；`'parent'` → `<Redirect href="/parent/children" />`；`'child'` → `<Redirect href="/child/welcome" />`
- [ ]  Role guard（在 `mobile/app/_layout.tsx` 解封后的 `RootLayoutNav` 内）：`segments[0]` 取值为 `'auth'`/`'parent'`/`'child'`/`'dev'`（无括号，F0.5 后形态）；hydrated 前不导航；`'dev'` 命中时不强制 role 匹配（dev 工具游离于 role guard 外）
- [ ]  新建 `mobile/app/dev/hub.tsx`：5 按钮（开始测试 / 清会话 / 重置 device / 打开展厅 `router.push('/dev/components')` / 打开 SSE 测试页 `router.push('/dev/chat')`）+ 调试信息面板（role / token / deviceId / route / API base）
- [ ]  新建 `mobile/app/dev/_layout.tsx`：`if (!__DEV__) throw new Error('dev routes are dev-only')`，作为生产构建二级保险（即使 M15 漏删目录也不会让 dev 路由可达）

**验证**：

- ✅ `START_AT_DEV_HUB=true` 冷启 → `/dev/hub`
- ✅ 改 false + Reload → 按 role 正常 bootstrap（无 token → `/auth/landing`）
- ✅ Dev Hub 五按钮行为符合预期
- ✅ `useSegments()[0]` 在 role guard 内取值无括号
- ❌ 生产构建（`__DEV__=false`）不得进入 `/dev/*` 路由

**提交**：`feat(mobile): bootstrap + role guard + Dev Hub (M5 F2)`

---

### Step F3：auth/landing + auth/login

`depends_on: [F2]`

- [ ]  `auth/landing.tsx`：Logo + Mascot idle + 两大按钮（我是家长 → `/auth/login`；我是孩子 · 扫码登录 → `/auth/bind/scan`） + 底部合规小字
- [ ]  `auth/login.tsx`：用户名 Input（`autoCapitalize="none"` + `autoCorrect={false}`，**不**用 `keyboardType="phone-pad"`——MVP `create_parent.py` 生成的 phone 是 4 位小写字段） + 密码 Input + 登录按钮
- [ ]  `POST /auth/login` body `{ phone, password, device_id }`；API client 同时统一注入 `X-Device-Id`，保持与现有鉴权依赖兼容
- [ ]  错误文案（按状态码兑底，不依赖 detail 文本）：401「账号或密码错误」/ 429「登录过于频繁，请稍后重试」/ 其他 4xx（含 422 入参校验失败）「登录失败，请检查输入」/ 5xx「网络异常，稍后重试」
- [ ]  成功 → `setSession({ role: "parent", token, userId: resp.account.id })` → role guard 自动跳 parent 区

**验证**：

- ✅ 冷启无 token → landing；登录成功 → children 列表
- ✅ **全局交互约定（Loading / Toast / 防重入 / 状态对齐）已落实**

**提交**：`feat(mobile): auth landing + parent login (M5 F3)`

---

### Step F4：parent/children 列表 + 建孩子表单

`depends_on: [F3, B0-quota]`（后端 API 由 M4.8 提供；3 子账号上限由 B0-quota 提供 server-side 拦截）

**页面设计示意（2026-04-28 评审拍板）**：

图中 `👦` / `👧` / `🗑` / `›` / `+` 仅为 ASCII 示意；实际渲染统一走 `@expo/vector-icons` Ionicons，icon 选型与配色见下方〈图标实现约定〉。

```jsx
┌──────────────────────────────────────────┐
│  我的孩子                          [ + ] │ ← header（+ 在 children.length>=3 时 disabled）
├──────────────────────────────────────────┤
│  ┌──────────────────────────────────┐    │
│  │  👦  小明 · 12岁               › │    │ ← 信息区（ListItem，整块 Pressable → settings）
│  │ ─────────────────────────────── │    │
│  │  [    下线设备             ] 🗑 │    │ ← 操作行（ActionRow，Card 子兄弟元素）
│  └──────────────────────────────────┘    │
│  ┌──────────────────────────────────┐    │
│  │  👧  小红 · 9岁                › │    │
│  │ ─────────────────────────────── │    │
│  │  [    绑定设备             ] 🗑 │    │
│  └──────────────────────────────────┘    │
└──────────────────────────────────────────┘

空态：Mascot 居中 + 引导文案「点右上 + 添加你的第一个孩子」
上限态（length===3）：右上 [+] disabled 灰显，点击 → Toast「已达上限（3 个），请先删除」
```

- [ ]  `parent/_layout.tsx`：Stack（M5 单页为主，不加 Tab）；header title 设为「我的孩子」
- [ ]  **`parent/children/index.tsx`** 列表页：
    - FlatList + 空态（Mascot + 引导文案）+ 下拉刷新 + `useFocusEffect(useCallback)` refetch（不能用 `useEffect`——modal/new 返回时组件不卸载）
    - 列表排序：用后端默认 created_at asc（新增孩子追加在底部，不打扰原有顺序）
    - 加载态：3 张 skeleton 占位卡（不要全屏 spinner）
    - API 错误态：EmptyState 变体 + 「点击重试」
    - 右上 [+] 按钮：`children.length >= 3` 时**视觉 disabled 灰显，但外层 Pressable 不真正 disabled**；`onPress` 内 guard 后 Toast「已达上限（3 个），请先删除」；正常态 → `router.push('/parent/children/new')`
- [ ]  **卡片结构**（彻底无嵌套 Pressable，全部复用 M4.5 现成组件）：

```jsx
<Card variant="outlined" padding={0}>
  {/* 信息区：复用 M4.5 ListItem，整块 Pressable → settings */}
  <ListItem
    leading={<GenderAvatar gender={g} size={48} />}
    title={`${nickname} · ${age}岁`}
    trailing={<Ionicons name="chevron-forward" size={20} color={s400} />}
    divider={true}
    onPress={handleOpenSettings /* F5 接入 settings 路由；F4 阶段可 no-op/toast 占位 */}
  />
  {/* 操作行：Card 子兄弟元素，不在 ListItem 内，零嵌套 Pressable */}
  <View style={styles.actionRow}>
    <Button
      variant="primary"
      size="md"
      style={styles.mainButton} /* { flex: 1 } */
      loading={mainBtn.loading}
      disabled={mainBtn.disabled}
      onPress={handlePrimaryAction /* F5 接入绑定/下线流程；F4 阶段可 no-op/toast 占位 */}
    >
      {mainBtn.label /* '绑定设备' | '下线设备' */}
    </Button>
    <Pressable
      onPress={handleDeletePress /* F5 接入删除确认；F4 阶段可 no-op/toast 占位 */}
      hitSlop={8}
      style={styles.trashIconButton}
    >
      <Ionicons name="trash-outline" size={20} color={redline} />
    </Pressable>
  </View>
</Card>
```

**关键点**：

- Card 是纯视觉容器（不 Pressable），承担圆角 / 边框 / shadow
- ListItem 自带 onPress，整个信息区可点击 → settings 占位页
- ActionRow 是 Card 的子兄弟元素，**不在 ListItem 内**，所以点 Button / 垃圾桶**不会触发 ListItem 的导航**——零 stopPropagation hack
- 年龄展示：**纯数字 + 「岁」**（如「12岁」「3岁」「20岁」），不走 `formatAgeLabel`；formatAgeLabel 只在 AgePicker 边界态用
- nickname 取首字（fallback 用，仅当 GenderAvatar 异常时）：`[...nickname][0]`（code point 安全，避免 emoji 半个代理对损坏）

**主按钮状态切换（is_bound 驱动）**：

| `is_bound` | 文案 | loading 文案 | onPress 行为 |
| --- | --- | --- | --- |
| false | 绑定设备 | 请求中… | 触发出码流程（POST /bind-tokens → 打开 BindQrModal，详见 F5） |
| true | 下线设备 | 下线中… | 打开 OfflineConfirmModal（详见 F5） |
- 不再有「绑定态徽章」（按钮文案本身就是状态指示）
- 主按钮 loading vs disabled 拆分严格按〈全局交互约定 § Loading 态〉

**垃圾桶按钮**：始终启用；F4 阶段只保留视觉与点击隔离结构，onPress 可 no-op/toast 占位；DeleteChildConfirmModal 打开与删除 API 接线统一放到 F5。

- [ ]  **`parent/children/new.tsx`** 建孩子表单：
    - 昵称 Input
    - AgePicker（M4.6 DiscreteSlider）
    - 性别选择：**三个 Avatar 大图（Boy / Girl / Unknown）+ 选中态边框**（与列表卡片视觉口径一致，不再是 Radio），default = `unknown`
    - [ ]  `POST /children` 成功 → Toast「已添加」（success 1.5s）+ `router.back()`
    - [ ]  失败处理：
        - 409 quota exceeded → Toast「最多创建 3 个孩子」+ 不退出表单
        - 422 字段校验失败 → 高亮对应字段
        - 其他 4xx → Toast「创建失败，请检查输入」
        - 5xx → Toast「网络异常，稍后重试」
- [ ]  **`mobile/components/business/AgePicker/`**：基于 M4.6 DiscreteSlider，`nodes=Array.from({length:19},(_,i)=>i+3)`；统一导出 `formatAgeLabel(age)`，`age <= 3` → `"3-"`，`age >= 21` → `"20+"`，中间 → `"{age}岁"`；左 label / 右 label / centerLabel 复用该函数；**列表卡片不复用此函数**
- [ ]  前端 `birth_date_to_age` 仅负责从后端 `birth_date` 算出数值年龄；解析 `YYYY-MM-DD` 时按日期字符串处理，避免 JS timezone 造成生日偏移一天

**图标实现约定（M5 引入；沿用现有依赖 `@expo/vector-icons`，不新增图标 / SVG 资源依赖）**：

M14 视觉打磨阶段如不满意可整体替换为自绘 SVG 风格头像，**`GenderAvatar` 组件 API 保持稳定**。

**`mobile/components/business/GenderAvatar/`**：

- props：`{ gender: 'male' | 'female' | 'unknown', size?: number, selected?: boolean }`，`size` 默认 48
- 结构：圆形 tint 背景 `View` + Ionicons icon 居中
- 三态映射：
    - `male` → bg `#DBEAFE`（蓝-100）+ Ionicons `man` color `#2563EB`（蓝-600）
    - `female` → bg `#FCE7F3`（粉-100）+ Ionicons `woman` color `#DB2777`（粉-600）
    - `unknown` → bg `var(--s100)` + Ionicons `help` color `var(--s500)`
- icon size 约为 `size * 0.58`（48 → 28；72 → 42）
- `selected===true` → 外层加 `border: 2px solid var(--p500)`（仅 `parent/children/new.tsx` 三选一形态使用；列表卡片不传 selected）
- 已知取舍：放弃了原方案的「🤫 嘘」语义彩蛋——vector icon 无对应表情；M14 视觉打磨阶段如需找回可切回自绘 SVG，`GenderAvatar` 组件 API 兼容

**列表卡片 / 表单 / 占位页其他图标**（统一 Ionicons，不混用其他 set）：

- 列表 header「+」按钮：`add` size 24，color `var(--s700)`（disabled 态 `var(--s400)`）
- 卡片右侧 chevron：`chevron-forward` size 20，color `var(--s400)`（在 ListItem trailing 内）
- 卡片操作行垃圾桶：`trash-outline` size 20，color `var(--redline)`，hitSlop 8（防误触；touch 区不要太大避免误点）
- settings 占位页：`construct-outline` size 48，color `var(--s400)`

**色值说明**：蓝-100 / 蓝-600 / 粉-100 / 粉-600 为临时硬编码（仅在 `GenderAvatar` 内联使用），M14 视觉打磨可纳入 design token；`var(--s*)` / `var(--p500)` / `var(--redline)` 已在 M4.5 视觉基线中定义。

**绑定态徽章砍掉决策（2026-04-28 评审拍板）**：

原方案在卡片信息区显示红色「未绑定」/ 灰带圆点「已绑定」徽章。本里程碑评审时由 Iver 拍板砍掉，理由：

- 主按钮文案「绑定设备」/「下线设备」**配对工整 + 语义对立明显**，状态从按钮文案就能 O(1) 识别
- 徽章 + 按钮文案双重指示信息冗余
- 「下线设备」按钮存在 = 已绑定，「绑定设备」按钮存在 = 未绑定，零额外认知成本

后续里程碑如果出现「需要在不显示主按钮的位置也指示绑定态」的场景再回头补徽章组件。

**验证**：

- ✅ 空 / 单 / 多孩子（3 个）三态视觉对齐示意图
- ✅ AgePicker 滑动流畅；创建提交后列表 refresh（useFocusEffect 生效）
- ✅ 创建第 4 个孩子（绕过前端校验，DevTools 强制点击）→ 后端 409 → Toast「最多创建 3 个孩子」
- ✅ `GenderAvatar` 三性别正确显示（Ionicons 圆形头像，跨平台视觉一致）
- ✅ `is_bound=false` 时主按钮显示「绑定设备」；`is_bound=true` 时显示「下线设备」
- ✅ 点 ListItem 信息区（含 chevron 区域）→ F4 阶段不触发主按钮 / 垃圾桶；settings 路由与跳转在 F5 接入后验证
- ✅ 点主按钮 / 垃圾桶 → F4 阶段不触发 ListItem 导航；对应 modal 打开与 API 接线在 F5 验证（结构天然隔离，无 stopPropagation）
- ✅ 卡片不出现绑定态徽章（已砍）
- ❌ 卡片不允许出现「年龄 3-岁 / 20+岁」（formatAgeLabel 误用）
- ✅ **全局交互约定（Loading / Toast / 防重入 / 状态对齐）已落实**

**提交**：`feat(mobile): children list with inline actions + new child form (M5 F4)`

---

### Step F5：BindQrModal + OfflineConfirmModal + DeleteChildConfirmModal + settings 占位

`depends_on: [F4]`（`DELETE /children` 与 `/bind-tokens/*` 端点均以 M4.8 合并后的契约为准；旧 M4 bind-token 路径不得再使用）

**结构变更（2026-04-28 评审拍板）**：

1. 原「孩子详情页 `[id]/index.tsx`」已砍——所有操作（绑定 / 下线 / 删除）已搬到 F4 列表卡片，详情页无内容可承载
2. 「强制下线」从 RN Alert 改为 M4.5 Modal（统一组件体系，便于挂 mutation loading 与失败兜底）
3. 本 Step 交付并接线：3 个 modal 组件（由 F4 卡片按钮挂载）+ 1 个信息区指向的 settings 占位页；F4 中的主按钮 / 垃圾桶 / ListItem 导航占位在本 Step 统一接入
- [ ]  `npx expo install react-native-qrcode-svg expo-clipboard`（`react-native-svg` 已由当前 Expo / M4.7 环境提供，不重复改版本）
- [ ]  **`parent/children/[id]/settings.tsx`** 占位页：
    - 页面标题「孩子设置」
    - 内容：居中 Ionicons `construct-outline`（size 48，color `var(--s400)`）+ 一句「设置功能开发中」+ 返回按钮
    - 路由保留，M6+ 直接填修改 nickname / 年龄 / 性别等表单
    - 不调任何 API

**BindQrModal**（`mobile/components/business/BindQrModal/`）：

- 触发位置：F4 列表卡片主按钮（`is_bound === false` 态文案「绑定设备」）
- props 契约：`{ visible: boolean, childId: string, nickname: string, bindToken: string, expiresInSeconds: number, onRefresh: () => Promise<{ bindToken: string, expiresInSeconds: number }>, onClose: (reason: 'bound' | 'user_close' | 'expired') => void }`
    - `bindToken` / `expiresInSeconds` 由父组件在打开前通过 `POST /bind-tokens` 获取，确保 modal 打开后可立即渲染二维码
    - `onRefresh` 由 modal 内刷新按钮调用，父组件重新 `POST /bind-tokens` 后回传新 token
- **打开流程（loading 责任链）**：
    1. F4 主按钮 `onPress` → 列表按钮 loading + disabled（按〈全局交互约定〉短场景规则）
    2. 父组件调 `POST /bind-tokens` body `{ child_user_id: childId }` → 拿到 `{ bind_token, expires_in_seconds: 300 }`
    3. POST 成功 → modal `visible=true`；列表按钮 loading 结束，仅保留 disabled（modal 打开期间不再转圈）
    4. POST 失败 → 不打开 modal；列表按钮释放；Toast「出码失败，请重试」error 3s
- **modal 内 UI**：
    - QR `value={bind_token}` + 等宽字体单行展示（22 字符 base64url）
    - 3s 轮询 `GET /bind-tokens/{bind_token}/status`：pending 继续；bound → 关 modal 走 `onClose('bound')`；expiresAt 到期 → 切过期态（灰显 QR + 刷新按钮）；用户点过期态关闭 → `onClose('expired')`
    - 底部按钮：刷新（重拿新 token，重置过期计时）+ 复制绑定码（`expo-clipboard.setStringAsync`）
- **关闭路径（3 种，必须全部走 onClose）**：

| 触发 | onClose reason | 父组件处理 |
| --- | --- | --- |
| 轮询命中 `bound` → modal 自动关 | `'bound'` | **不 refetch**；本地切 `is_bound=true`  • Toast「已绑定」success 1.5s |
| 用户主动关（点遮罩 / 关闭按钮） | `'user_close'` | refetch GET /children；diff `is_bound`：false→true 弹「已绑定」success 1.5s / 否则不弹 |
| 过期态用户点关闭 | `'expired'` | refetch GET /children；diff 同上 |
- **关闭副作用**：清 polling interval；释放列表按钮 disabled；卸载时 interval 必须清掉
- **过期态 UI**：5 分钟到期 → modal 内 QR 灰显 + 「绑定码已过期」文字 + 刷新按钮（用户可重拿新 token，重置过期计时）

**OfflineConfirmModal**（`mobile/components/business/OfflineConfirmModal/`，新增）：

- 触发位置：F4 列表卡片主按钮（`is_bound === true` 态文案「下线设备」）
- props 契约：`{ visible: boolean, childId: string, nickname: string, onConfirm: () => void, onCancel: () => void }`
- **modal 内 UI**：
    - 标题「确认下线」
    - 警告文案「下线后「{nickname}」当前登录的设备将被立即踢出，需要重新绑定才能再次使用。」
    - 取消按钮 + 「确认下线」danger 按钮
- **「确认下线」按钮 onPress**（按〈全局交互约定〉决策型 modal 内 confirm 不带 loading）：
    1. 立刻 `onConfirm()` → 父组件关 modal + 列表按钮 loading + disabled
    2. 父组件发起 `POST /children/{id}/revoke-tokens`
    3. 成功（204 即权威，无响应体）→ 按当前 `childId` 本地更新 `is_bound=false` + Toast「已下线」success 1.5s + 列表按钮释放（自动切回「绑定设备」文案）
    4. 失败 → 列表按钮释放（保持「下线设备」文案）+ Toast「下线失败，请稍后重试」error 3s
- **不 refetch**（204 成功状态即权威，按当前 `childId` 本地切状态）

**DeleteChildConfirmModal**（`mobile/components/business/DeleteChildConfirmModal/`）：

- 触发位置：F4 列表卡片垃圾桶按钮
- props 契约：`{ visible: boolean, childId: string, nickname: string, onConfirm: () => void, onCancel: () => void }`
- **modal 内 UI**：
    - 标题「永久删除」
    - 合规文案（按「依据 → 行为 → 不可恢复 → 操作指引」结构）：
    
    > 根据《个人信息保护法》《未成年人网络保护条例》对监护人删除权的要求，我们将对「{nickname}」的账号、聊天记录、日报、通知等所有个人信息进行**永久删除**，**不可恢复**。
    
    请在下方输入孩子昵称「**{nickname}**」以激活删除按钮。
    > 
    - Input（受控，trim 比较）+ 取消按钮 + 「确认删除」danger 按钮（input 严格匹配 nickname 才激活）
- **「确认删除」按钮 onPress**（同样不带 modal 内 loading）：
    1. 立刻 `onConfirm()` → 父组件关 modal + 列表 mutation 状态进入 deleting（卡片可灰显或保留原貌按 UX 选择，最简方案：保留原貌等响应）
    2. 父组件发起 `DELETE /children/{id}`
    3. 成功（204）→ 列表本地移除该卡片 + Toast「已删除」success 1.5s
    4. 失败 → 列表卡片仍在 + Toast「删除失败，请稍后重试」error 3s
- **不 refetch**（本地移除即可）

**验证**：

- ✅ QR 可被子端扫出
- ✅ BindQrModal 打开期间，F4 列表对应主按钮 disabled 但**不转 spinner**
- ✅ 关 modal（任意路径）→ 父组件 onClose 收到正确 reason；polling interval 清掉；卸载亦清
- ✅ `reason='bound'` 路径**不调** GET /children；`'user_close' / 'expired'` 路径调一次
- ✅ 用户在 3s 轮询空档手动关 modal（孩子刚扫完）→ refetch 拿到 is_bound=true → 按钮自动切「下线设备」+ Toast「已绑定」（覆盖空档期边界）
- ✅ 删除成功后列表本地无该孩子（不依赖 refetch）
- ✅ 下线成功后列表卡片主按钮自动切回「绑定设备」（基于 mutation 返回值，不 refetch）
- ✅ 点击列表卡片 ListItem 信息区 → settings 占位页正常显示，返回回到列表
- ✅ DeleteChildConfirmModal 文案严格按上方草案（法规依据前置 + 操作指引）
- ✅ 三个 modal 内的「确认」按钮**均不显示 loading**（loading 由列表按钮承载）
- ✅ **全局交互约定（Loading / Toast / 防重入 / 状态对齐）已落实**

**提交**：`feat(mobile): bind qr + offline confirm + delete confirm modals + settings placeholder (M5 F5)`

---

### Step F6：auth/bind/scan + manual + child/welcome

`depends_on: [F2]`（依赖显式路由命名空间、bootstrap 与 role guard；GET /me/profile 由 M4.8 提供）

- [ ]  `npx expo install expo-camera`；配置相机权限文案，真机验证首次授权 / 拒绝 / 二次进入
- [ ]  `auth/bind/scan.tsx`：`useCameraPermissions()` 首挂载请求；`CameraView barcodeScannerSettings={ barcodeTypes: ['qr'] }`；扫到结果直接 = 完整 bind_token（不需字符串处理）；`scanLock = useRef(false)` 防重入；权限拒绝 → EmptyState + 跳 manual；底部常驻「手动输入绑定码」按钮
- [ ]  `auth/bind/manual.tsx`：Input（等宽 + `autoCapitalize="none"` + `autoCorrect={false}`，**仅过滤空格**，不做大小写转换——bind_token 是 base64url 区分大小写） + 确认按钮
- [ ]  两页均调 `POST /bind-tokens/{bind_token}/redeem` body `{ device_id }` → `LoginResponse { token, account: AccountOut }` → `setSession({ role: "child", token, userId: account.id })` → `router.replace('/child/welcome')`
- [ ]  失败 → Toast「绑定码无效或已失效」+ 解锁 scanLock 重试

**child/welcome.tsx**：

- 挂载 `useEffect` 调 `GET /me/profile` → `setNickname(resp.nickname)`
- Mascot 仅作为静态展示元素使用：按当前项目实现随机眨眼，不依赖 `state` / `onFinish` 切换；如需入场感，用页面级淡入，不改 Mascot 内部
- 大字「嗨 {nickname}，我是小盒子！」；底部小字「聊天能力将在后续版本开放」
- nickname 未返回（异常路径）→ 显示「嗨，我是小盒子！」
- 不接输入 / 不发其他请求（M7 替换）

**验证**：

- ✅ 真机扫码 → redeem → welcome 欢迎语含 nickname；manual 粘贴势同路径；权限拒绝有兜底
- ✅ **全局交互约定（Loading / Toast / 防重入 / 状态对齐）已落实**

**提交**：`feat(mobile): bind scan/manual + child welcome shell (M5 F6)`

---

### Step F7：单机 + 真机双设备闭环验收

`depends_on: [F1..F6]`

**单机闭环**（一机跑完）：

1. `START_AT_DEV_HUB=true` 启动 → Dev Hub → 开始测试 → landing → 家长 → login
2. 运维 CLI 预建父账号：`python -m app.scripts.create_parent --note "m5 测试"`（脱机生成 4 位小写字段 phone + 8 位随机密码）→ 登录
3. children 空态 → 建孩子（昵称 + 年龄 + 性别）→ 列表出现未绑定卡
4. 列表卡片「📷 扫码绑定」→ 弹 BindQrModal → 复制绑定码
5. Dev Hub 清会话 → 开始测试 → landing → 孩子 → bind/scan → 手动输入 → 粘贴 → welcome（欢迎语含 nickname）
6. Dev Hub 重置 device → 验证 deviceId 变化
7. 试图建第 4 个孩子（绕过前端 disabled 校验）→ 后端 409 → Toast「最多创建 3 个孩子」

**真机双设备闭环**：

- 设备 A 父端建孩子 + 出码；设备 B 子端扫码进 welcome；设备 A 列表刷新后 `is_bound=true`

**删除闭环**（依赖 M4.8 B6 已落地）：

- 父端永久删除 → 输入 nickname 匹配 → 确认
- DB 核：CASCADE 后 [技术架构讨论记录（持续更新）](https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420?pvs=21) §八列出的一/二级 FK 链全空
- `data_deletion_requests` 新增一行：`child_id_snapshot` 匹配 / `deleted_tables` JSONB / `reason='parent_request'` / `created_at` 非空
- 不受影响：families / parent users / parent family_members / `notifications.child_user_id IS NULL` 的系统通知
- Redis：该 child 的 `auth:{token_hash}` 缓存已清（设备 B 后续请求即时 401）

验收报告 `mobile/docs/m5-acceptance-report.md`（含截图 + DB / Redis 核查结果）

**提交**：`docs(m5): acceptance report (M5 F7)`

---

## 验收清单

- ✅ 后端：M4.8 已合并主线，`uv run alembic upgrade head` 成功；`uv run pytest` 全绿
- ✅ 前端：`npm run lint` + `npm run typecheck` 通过
- ✅ 三条闭环（单机 / 真机双设备 / 删除）全部走通
- ✅ 验收报告归档到 `mobile/docs/m5-acceptance-report.md`

## 发现与建议

- Tag 通用组件抽象**已被本里程碑反驳**：HTML 原型 v1.2 没有 5 档语义 Tag 范式，强造抽象浪费 token + 新增局部 colors.ts。后续里程碑积累足够语义场景再回头抽组件。
- Mascot 在 M5 只按当前静态随机眨眼能力展示，不恢复 M4.7 已砍掉的状态化动效。
- **M4.8 后端核查（2026-04-28）**：源码核查 `backend/app/api/{bind_tokens,children}.py` + `backend/app/models/accounts.py` 后**撤回**原「bind-tokens 补 Redis 缓存」建议——该端点本身是 Redis 原生（`GET bind_result:*` + `EXISTS bind_token:*`），零 DB 查询，不存在缓存层语义。但核查顺手发现真隐患：`auth_tokens` 表零索引（`token_hash` 鉴权热路径全表扫 P0 + `user_id` `GET /children` EXISTS 全表扫 P1），已登记为 M4.8 收口后 hotfix-1（独立分支 `fix/m4.8-hotfix-auth-tokens-index`，详见 [M4.8 · 执行偏差记录](https://www.notion.so/M4-8-d745c53c51564a938fec0df3aab62187?pvs=21) §收口后 hotfix · auth_tokens 索引补齐）。不阻塞 M5。
- **Toast 任务栏（snackbar with progress）能力**：本里程碑评估了「长挂头部任务栏 + 状态切换」模式（点完即走，状态自查，多任务并行），认为 M5 单一受益场景（下线设备）不值得为之扩展 M4.5 Toast。已记入 [](https://www.notion.so/08702b0844724c1eaeb4707fe8f2f72e?pvs=21)，等 M7 LLM 长流式响应触发再做（届时多场景受益，做基建合算）。
- 执行中出现与计划不一致、环境适配、阻塞诊断、范围外发现，统一记录到 `M5 · 执行偏差记录` 子页；计划页只勾选完成项。