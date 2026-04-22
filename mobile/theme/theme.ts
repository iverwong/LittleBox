import { palette, ui, report, tags, fontSize, fontWeight, lineHeight, spacing, radius, shadow, surface } from './tokens'

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
} as const

export type Theme = typeof theme
