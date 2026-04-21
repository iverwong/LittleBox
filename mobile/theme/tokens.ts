// mobile/theme/tokens.ts
// 所有数值 1:1 对应 HTML 原型 v0.5 中 :root 的 CSS variables。
// 调整流程：先在 HTML 原型迭代（版本号 +1）→ 回写本文件。

export const palette = {
	primary: {
		50: '#FAF2EA', 100: '#F0DFCB', 200: '#E2C4A5', 300: '#D2A882', 400: '#BE8B63',
		500: '#A67148', 600: '#8A5A36', 700: '#6D4627', 800: '#51331B', 900: '#352010',
	},
	secondary: {
		50: '#EEF2EE', 100: '#D6DFD6', 200: '#B2C2B3', 300: '#8FA491', 400: '#738A77',
		500: '#5C735F', 600: '#485C4B', 700: '#374739', 800: '#273327', 900: '#1A211A',
	},
	neutral: {
		50: '#F2EADF', 100: '#E8DDCD', 200: '#D4C4AE', 300: '#B8A587', 400: '#998260',
		500: '#7A6546', 600: '#5D4C33', 700: '#423623', 800: '#2B2216', 900: '#1A140B',
	},
} as const

// UI 反馈色：组件态（Input error / Toast / Button danger 等）
export const ui = {
	error: '#C56B5E',
	warning: '#D89155',
	success: '#7A9180',
	info: '#6A8CA8',
} as const

// 家长端报告分类色：仅用于日报 / 图表分类感知，孩子端 UI 禁止引用
export const report = {
	crisis: '#C56B5E',
	redline: '#D89155',
	guidance: '#6A8CA8',
	safe: '#7A9180',
} as const

// 类别标签色（8 色，同亮度低饱和）
export const tags = {
	t1: '#8AA894', t2: '#8AA3B8', t3: '#D4A857', t4: '#C47A6D',
	t5: '#A497B8', t6: '#9A9287', t7: '#B8876A', t8: '#8AB8A8',
} as const

// 字号阶
export const fontSize = { xs: 12, sm: 14, base: 16, md: 18, lg: 20, xl: 24, '2xl': 28, '3xl': 32 } as const
export const fontWeight = { regular: '400', medium: '500', semibold: '600', bold: '700' } as const
export const lineHeight = { tight: 1.25, normal: 1.5, relaxed: 1.75 } as const

// 间距（4 的倍数）
export const spacing = { 0: 0, 1: 4, 2: 8, 3: 12, 4: 16, 5: 20, 6: 24, 8: 32, 10: 40, 12: 48, 16: 64 } as const

// 圆角（对齐 HTML 原型 v0.5）
export const radius = { none: 0, sm: 8, md: 12, lg: 16, xl: 20, '2xl': 28, full: 9999 } as const

// 阴影（移植自 HTML 原型 v0.5；RN 不支持 spread，shadowRadius 近似）
export const shadow = {
	sm: { shadowColor: '#3C2814', shadowOpacity: 0.05, shadowOffset: { width: 0, height: 1 }, shadowRadius: 2, elevation: 1 },
	md: { shadowColor: '#3C2814', shadowOpacity: 0.08, shadowOffset: { width: 0, height: 2 }, shadowRadius: 6, elevation: 3 },
	lg: { shadowColor: '#3C2814', shadowOpacity: 0.10, shadowOffset: { width: 0, height: 4 }, shadowRadius: 14, elevation: 6 },
} as const
