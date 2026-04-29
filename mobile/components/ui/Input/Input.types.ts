import type { StyleProp, ViewStyle } from 'react-native'
import type { FeatherName } from '../types'

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
	autoCapitalize?: 'none' | 'sentences' | 'words' | 'characters'
	autoCorrect?: boolean
	disabled?: boolean
	onBlur?: () => void
	style?: StyleProp<ViewStyle>
}
