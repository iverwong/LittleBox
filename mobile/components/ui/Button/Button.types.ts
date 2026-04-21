import type { Feather } from '@expo/vector-icons'
import type { StyleProp, ViewStyle } from 'react-native'

export type FeatherName = keyof typeof Feather.glyphMap

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md' | 'lg'

export interface ButtonProps {
	variant?: ButtonVariant
	size?: ButtonSize
	loading?: boolean
	disabled?: boolean
	leftIcon?: FeatherName
	rightIcon?: FeatherName
	onPress?: () => void
	children?: React.ReactNode
	style?: StyleProp<ViewStyle>
}
