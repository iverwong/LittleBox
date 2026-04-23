import { palette, ui, report, tags, fontSize, fontWeight, lineHeight, spacing, radius, shadow, surface, layout } from './tokens'

export const theme = {
	palette,
	ui,
	report,
	tags,
	typography: { fontSize, fontWeight, lineHeight },
	spacing,
	radius,
	shadow,
	surface,
	layout,
} as const

export type Theme = typeof theme
