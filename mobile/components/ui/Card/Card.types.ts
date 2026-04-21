import type { StyleProp, ViewStyle } from 'react-native'

export type CardVariant = 'elevated' | 'outlined' | 'filled' | 'accentSecondary'

export interface CardProps {
	variant?: CardVariant
	/** Padding index into theme.spacing (e.g. 4 = 16px). Defaults to 4. */
	padding?: number
	onPress?: () => void
	children: React.ReactNode
	style?: StyleProp<ViewStyle>
}
