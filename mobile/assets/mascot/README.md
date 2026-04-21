# Mascot Assets（占位阶段）

本目录在占位阶段**不存放任何图片资源**。占位视觉完全由 `components/mascot/Mascot.tsx` 组件自渲染（View + Feather icon）。

## 最终替换步骤

当外包交付 Lottie JSON 到位后，按以下步骤替换（最小化波及范围）：

1. 将外包 `mascot-littlebox-v1/export/states/` 的 6 个 JSON 和 `transitions/` 的 7 个 JSON（共 13 个）放入 `mobile/assets/mascot/lottie/`
2. 在 `mobile/` 执行 `npm i lottie-react-native`（确认最新稳定版）
3. 重写 `mobile/components/mascot/Mascot.tsx` 内部实现（Props / `MascotState` / `MascotSize` / `onFinish` 接口**保持不变**）

**调用方 API 契约不变**，仅替换 `Mascot.tsx` 内部实现。

## 外包交付契约

详见 `docs/M4.5-plan.md`「§吉祥物集成基线（外包交付契约）」章节。

关键约束：
- 格式：Lottie JSON（AE + Bodymovin 导出）
- 集成库：`lottie-react-native`
- 画布：1024×1024（1:1）；实际渲染 48–200px；30 fps
- 背景：透明，禁任何底色
- 状态：6 个（enter / idle / listen / thinking / narrating / done）+ 7 个过渡
- 调色板：`palette.primary` 范围内（#FAF2EA → #352010）
