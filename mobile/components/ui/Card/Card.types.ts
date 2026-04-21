import type { StyleProp, ViewStyle } from 'react-native'

export type CardVariant = 'elevated' | 'outlined' | 'filled' | 'accentSecondary'

// padding 值约束到 theme.spacing 的 key，类型安全 + 查表走 theme.spacing
export type CardPadding = 0 | 1 | 2 | 3 | 4 | 5 | 6 | 8 | 10 | 12 | 16

export interface CardProps {
	variant?: CardVariant
	/** Padding index into theme.spacing (e.g. 4 = 16px). Defaults to 4. */
	padding?: CardPadding
	onPress?: () => void
	children: React.ReactNode
	style?: StyleProp<ViewStyle>
}
