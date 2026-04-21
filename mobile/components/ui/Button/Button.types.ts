import type { StyleProp, ViewStyle } from 'react-native'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md' | 'lg'

export interface ButtonProps {
	variant?: ButtonVariant
	size?: ButtonSize
	loading?: boolean
	disabled?: boolean
	leftIcon?: string
	rightIcon?: string
	onPress?: () => void
	children?: React.ReactNode
	style?: StyleProp<ViewStyle>
}
