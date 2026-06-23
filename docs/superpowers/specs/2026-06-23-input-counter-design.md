# Input Counter 视觉优化

**日期:** 2026-06-23
**范围:** `mobile/components/ui/Input/`
**目标:** 消除 counter 出现时导致的 input 抬高，优化颜色分级，增加 100% 抖动提示。

---

## 现状问题

Counter 当前定位在 input 容器右上角（`position: absolute, top: 0`）。为保证文字不与 counter 重叠，在 counter 出现时动态给 TextInput 加 `paddingTop: 14, paddingBottom: 14`，覆盖默认的 size padding（md: 8px, lg: 12px）。这个动态 padding 切换导致 input 高度突变，用户看到框"跳"一下。

## 设计

### 1. 布局：counter 移至右下角 + 恒定 paddingBottom

**原则：** 只要 `showCount && maxLength` 启用，TextInput 从一开始就给恒定 `paddingBottom: 18`，不再动态变化。Counter 的显隐仅做 `opacity` 切换，布局零变化。

**改动：**

`Input.styles.ts`：
- `counter` 样式：`top: 0` → `bottom: 2`

`Input.tsx`：
- 删掉动态 paddingTop/paddingBottom 切换逻辑，改为恒定 `paddingBottom: 18`（仅当 `showCount && maxLength` 启用时）
- counter 始终渲染，`opacity` 在 80% 以下为 0，>=80% 为 1
- 移除 `counterError` 的 `fontWeight: semibold`（=100% 不再用粗体强调，改为抖动）

### 2. 颜色分级

现有逻辑已匹配需求，仅注释对齐：

| 区间 | 变体 | 颜色 | 说明 |
|------|------|------|------|
| < 80% | — | opacity: 0 | 用户无感知压力时不显示 |
| [80%, 90%) | normal | `neutral[300]` | 温和存在感 |
| [90%, 100%) | warn | `ui.error` | danger 色提醒 |
| = 100% | error | `ui.error` | danger + 抖一下 |

### 3. 抖动动画（=100% 瞬间触发一次）

**触发条件：** `ratio` 从 `< 1` 变为 `= 1` 时触发一次。持续保持在 100% 不重复抖。

**实现：**
- `useRef(new Animated.Value(0))` 存动画值
- `Animated.sequence` 编排 6 帧衰减序列：六段 `Animated.timing`，toValue 分别 1 → -1 → 0.6 → -0.6 → 0.3 → 0，总时长约 290ms
- `inputRange: [-1, 1]` → `outputRange: [-6, 6]`，即最大偏移 ±6px
- `useNativeDriver: true`，原生线程执行，不影响输入流畅度
- counter 元素从 `<Text>` 改为 `<Animated.Text>` 并绑定 `translateX`

### 4. 不改的

- `maxLength` 依然传给 TextInput，系统层截断保证不超限
- `showCount` prop 接口不变
- `Input.types.ts` 仅在注释上对齐新行为
- 使用方 `settings.tsx` 零改动

---

## 涉及文件

| 文件 | 改动 |
|------|------|
| `mobile/components/ui/Input/Input.styles.ts` | counter 定位改 bottom；counterWarn 色值对齐 |
| `mobile/components/ui/Input/Input.tsx` | 恒定 paddingBottom、opacity 显隐、抖动动画 |
| `mobile/components/ui/Input/Input.types.ts` | 注释对齐（可选） |
