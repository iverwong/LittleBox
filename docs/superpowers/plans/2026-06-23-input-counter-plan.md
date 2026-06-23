# Input Counter 视觉优化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 counter 出现时 input 抬高问题，优化颜色分级，增加 100% 时快速衰减抖动。

**Architecture:** Counter 从右上角移至右下角绝对定位；TextInput 在有计数功能时恒定 `paddingBottom: 18`；counter 始终渲染仅切换 opacity；到达 maxLength 瞬间触发一次 Animated.sequence 6 帧衰减左右抖动。

**Tech Stack:** React Native (Animated API, useNativeDriver), TypeScript

## Global Constraints

- 不影响 `showCount` / `maxLength` 的现有 prop 接口
- 使用方（`settings.tsx`）零改动
- `maxLength` 仍传给 TextInput 做系统层截断
- 原生驱动动画，不影响 JS 线程输入流畅度

---

### Task 1: 样式层 — counter 定位修正 + 清理

**Files:**
- Modify: `mobile/components/ui/Input/Input.styles.ts:81-94`

**Interfaces:**
- Produces: `counter` 样式改为 `bottom: 2`；`counterError` 移除 `fontWeight: semibold`

- [ ] **Step 1: 改 counter 定位 + 移除 fontWeight**

```typescript
// Input.styles.ts — counter 定义区域（约第 81-94 行），替换为：

counter: {
  // 浮在 input 容器内右下角，绝对定位，不参与布局流。
  // 位置在 paddingBottom 安全区上方，文字不会与之重叠。
  position: 'absolute',
  bottom: 2,
  right: theme.spacing[3],
  fontSize: theme.typography.fontSize.xs,
  color: theme.palette.neutral[300],
},
counterWarn: {
  // [90%, 100%)：danger 色提醒
  color: theme.ui.error,
},
counterError: {
  // =100%：danger 色 + 抖动（动画由组件层处理），不再用粗体强调
  color: theme.ui.error,
},
```

- [ ] **Step 2: 提交**

```bash
git add mobile/components/ui/Input/Input.styles.ts
git commit -m "style(Input): counter 改为右下角定位，移除 counterError fontWeight"
```

---

### Task 2: 组件层 — 恒定 paddingBottom + opacity 显隐 + 去"已达限额"

**Files:**
- Modify: `mobile/components/ui/Input/Input.tsx:1-148`

**Interfaces:**
- Consumes: 样式 `counter` 已定位 `bottom: 2`（Task 1）

- [ ] **Step 1: 动态 padding 改为恒定 paddingBottom**

当前代码（约第 105-116 行）：
```tsx
style={[
  styles.input,
  size === "lg" ? styles.size_lg : styles.size_md,
  { color: textColor },
  // counter 浮在 input 容器右上角,需要 paddingTop/Bottom 对称让出空间,
  // 不让 text 第一行与 counter 重叠、且 text 保持垂直居中
  renderCounter ? { paddingTop: 14, paddingBottom: 14 } : null,
  multiline && {
    minHeight: numberOfLines * 22,
    textAlignVertical: "top",
  },
]}
```

改为：
```tsx
// 计数功能启用时恒定 paddingBottom，counter 右下角透明/可见切换，布局零变化
{ color: textColor },
showCounter ? { paddingBottom: 18 } : null,
```

完整替换后：
```tsx
style={[
  styles.input,
  size === "lg" ? styles.size_lg : styles.size_md,
  { color: textColor },
  showCounter ? { paddingBottom: 18 } : null,
  multiline && {
    minHeight: numberOfLines * 22,
    textAlignVertical: "top",
  },
]}
```

- [ ] **Step 2: counter 条件渲染 → opacity 切换**

当前代码（约第 126-143 行）：
```tsx
{renderCounter && (
  <Text
    style={[
      styles.counter,
      counterVariant === "warn" && styles.counterWarn,
      counterVariant === "error" && styles.counterError,
    ]}
    pointerEvents="none"
    accessibilityLabel={...}
  >
    {value.length}/{maxLength}
    {counterVariant === "error" ? "  已达限额" : ""}
  </Text>
)}
```

改为：
```tsx
{showCounter && (
  <Text
    style={[
      styles.counter,
      counterVariant === "warn" && styles.counterWarn,
      counterVariant === "error" && styles.counterError,
      { opacity: renderCounter ? 1 : 0 },
    ]}
    pointerEvents="none"
    accessibilityLabel={
      ratio >= 1
        ? `已输入 ${value.length} 字，已达上限`
        : `已输入 ${value.length} 字，上限 ${maxLength}`
    }
  >
    {value.length}/{maxLength}
  </Text>
)}
```

注意：移除了"已达限额"后缀文字（抖动已替代此提示），counter 文本只显示 `N/M`。

- [ ] **Step 3: 提交**

```bash
git add mobile/components/ui/Input/Input.tsx
git commit -m "refactor(Input): 恒定 paddingBottom + opacity 显隐，去已达限额文字"
```

---

### Task 3: 抖动动画 — Animated.sequence 6 帧衰减

**Files:**
- Modify: `mobile/components/ui/Input/Input.tsx:1-148` — import 头部 + counter 元素

**Interfaces:**
- Consumes: Task 2 的恒定 paddingBottom + opacity 显隐

- [ ] **Step 1: 引入 Animated API，添加抖动逻辑**

import 头部（第 1-7 行）改动：

```tsx
import { TextInput, View, Text, Animated } from "react-native";
import { useMemo, useState, useCallback, useRef, useEffect } from "react";
```

**关键：** `Animated` 从 `react-native` 导入；`useRef`、`useEffect` 加入 react import。

计数器逻辑区（约第 54-66 行）后追加抖动逻辑：

```tsx
// —— 抖动动画（=100% 瞬间触发一次，6 帧快速衰减，原生线程驱动）——
const shakeAnim = useRef(new Animated.Value(0)).current;
const prevRatioRef = useRef(ratio);
useEffect(() => {
  // 仅在"刚好从 <1 变为 1"的那一刻触发
  if (ratio >= 1 && prevRatioRef.current < 1) {
    shakeAnim.setValue(0);
    Animated.sequence([
      Animated.timing(shakeAnim, {
        toValue: 1,
        duration: 50,
        useNativeDriver: true,
      }),
      Animated.timing(shakeAnim, {
        toValue: -1,
        duration: 50,
        useNativeDriver: true,
      }),
      Animated.timing(shakeAnim, {
        toValue: 0.6,
        duration: 50,
        useNativeDriver: true,
      }),
      Animated.timing(shakeAnim, {
        toValue: -0.6,
        duration: 50,
        useNativeDriver: true,
      }),
      Animated.timing(shakeAnim, {
        toValue: 0.3,
        duration: 50,
        useNativeDriver: true,
      }),
      Animated.timing(shakeAnim, {
        toValue: 0,
        duration: 40,
        useNativeDriver: true,
      }),
    ]).start();
  }
  prevRatioRef.current = ratio;
}, [ratio]);
```

动画幅度 `±6px` 是微妙级提示，6 帧总时长约 290ms，快速不扰人。`useNativeDriver: true` 保证原生线程执行，不影响 TextInput 输入。

- [ ] **Step 2: counter 元素改为 Animated.Text + translateX 绑定**

将 counter 渲染区（Task 2 中已改为非条件渲染的版本）的 `<Text>` 改为 `<Animated.Text>`，并绑定 `transform`：

```tsx
{showCounter && (
  <Animated.Text
    style={[
      styles.counter,
      counterVariant === "warn" && styles.counterWarn,
      counterVariant === "error" && styles.counterError,
      { opacity: renderCounter ? 1 : 0 },
      {
        transform: [
          {
            translateX: shakeAnim.interpolate({
              inputRange: [-1, 1],
              outputRange: [-6, 6],
            }),
          },
        ],
      },
    ]}
    pointerEvents="none"
    accessibilityLabel={
      ratio >= 1
        ? `已输入 ${value.length} 字，已达上限`
        : `已输入 ${value.length} 字，上限 ${maxLength}`
    }
  >
    {value.length}/{maxLength}
  </Animated.Text>
)}
```

- [ ] **Step 3: 提交**

```bash
git add mobile/components/ui/Input/Input.tsx
git commit -m "feat(Input): counter 100% 瞬间 6 帧衰减抖动动画"
```

---

### Task 4: 类型文件注释对齐（可选）

**Files:**
- Modify: `mobile/components/ui/Input/Input.types.ts:25-32`

- [ ] **Step 1: 更新 showCount 注释**

当前：
```tsx
/**
 * 是否在输入框右下角显示字符计数。
 * 需同时传 maxLength 才生效。
 * - < 80% 灰色
 * - >= 80% 橙色
 * - >= 100% 红色,并附加"已达限额"提示
 */
```

改为：
```tsx
/**
 * 是否启用字符计数（需同时传 maxLength）。
 * - < 80% 不显示
 * - [80%, 90%) neutral[300] 温和提示
 * - [90%, 100%) danger 色
 * - = 100% 触发一次快速抖动
 */
showCount?: boolean;
```

- [ ] **Step 2: 提交**

```bash
git add mobile/components/ui/Input/Input.types.ts
git commit -m "docs(Input): showCount 注释对齐新行为"
```

---

### Task 5: 端到端验证

- [ ] **Step 1: TypeScript 编译检查**

```bash
cd mobile && npx tsc --noEmit 2>&1 | head -30
```

预期：无新增类型错误。

- [ ] **Step 2: 在 settings.tsx 中手动测试**

在模拟器/真机上打开家长端 → 孩子配置页：
1. 昵称输入框（maxLength=12），输入到第 9 个字（9/12 = 75%）：counter 不可见
2. 输入第 10 个字（83%）：counter 右下角出现，`neutral[300]` 色
3. 输入第 11 个字（92%）：counter 变 danger 色
4. 输入第 12 个字（100%）：counter 抖一下，danger 色
5. 退格（99%）：counter 回 danger 色，不抖
6. 删除到 9 个字以下：counter 消失
7. 整个过程中 input 框高度不变

多行输入框（concerns, customRedlines, maxLength=500）同理测试。

- [ ] **Step 3: 提交（如有微调）**

```bash
git diff
# 如有必要修正，提交
```
