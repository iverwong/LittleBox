/** Layout constants shared across screens (header, tab bar, etc.) */
export const layout = {
	/** Default custom header height (matches expo-router stack header) */
	headerHeight: 56,
} as const

export type Layout = typeof layout
