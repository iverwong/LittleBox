// M4.7 新增 — 吉祥物专用色板，直接解构使用
// 与 theme.palette / theme.ui 等平级；通过 @/theme/colors 路径导入
import {
	palette,
	ui,
	report,
	tags,
	shadow,
	surface,
	mascot,
} from './tokens'

export const colors = {
	palette,
	ui,
	report,
	tags,
	shadow,
	surface,
	mascot,
} as const
