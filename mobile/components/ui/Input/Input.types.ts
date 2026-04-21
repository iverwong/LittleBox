import type { StyleProp, ViewStyle } from 'react-native'

export type InputSize = 'md' | 'lg'

export interface InputProps {
	value: string
	onChangeText: (text: string) => void
	placeholder?: string
	label?: string
	error?: string
	leftIcon?: string
	rightIcon?: string
	size?: InputSize
	secureTextEntry?: boolean
	keyboardType?: 'default' | 'email-address' | 'numeric' | 'phone-pad' | 'url'
	disabled?: boolean
	style?: StyleProp<ViewStyle>
}
