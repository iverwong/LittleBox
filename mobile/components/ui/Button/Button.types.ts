import type { StyleProp, ViewStyle } from 'react-native'
import type { FeatherName } from '../types'

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
