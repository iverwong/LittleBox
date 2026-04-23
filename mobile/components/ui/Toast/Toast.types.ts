import type { StyleProp, ViewStyle } from 'react-native'
import type { ToastVariant } from './toastStore'

export interface ToastProps {
	message: string
	variant: ToastVariant
	style?: StyleProp<ViewStyle>
}
