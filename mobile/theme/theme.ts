import { palette, ui, report, tags, fontSize, fontWeight, lineHeight, spacing, radius, shadow } from './tokens'

export const theme = {
	palette,
	ui,
	report,
	tags,
	typography: { fontSize, fontWeight, lineHeight },
	spacing,
	radius,
	shadow,
} as const

export type Theme = typeof theme
