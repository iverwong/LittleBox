import type { Feather } from '@expo/vector-icons'
import type { StyleProp, ViewStyle } from 'react-native'

export type FeatherName = keyof typeof Feather.glyphMap

export type InputSize = 'md' | 'lg'

export interface InputProps {
	value: string
	onChangeText: (text: string) => void
	placeholder?: string
	label?: string
	error?: string
	leftIcon?: FeatherName
	rightIcon?: FeatherName
	size?: InputSize
	secureTextEntry?: boolean
	keyboardType?: 'default' | 'email-address' | 'numeric' | 'phone-pad' | 'url'
	disabled?: boolean
	style?: StyleProp<ViewStyle>
}
