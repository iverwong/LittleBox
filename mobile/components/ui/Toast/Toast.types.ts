import type { StyleProp, ViewStyle } from 'react-native'
import type { ToastVariant } from './toastStore'

export interface ToastProps {
	message: string
	variant: ToastVariant
	onDismiss: () => void
	style?: StyleProp<ViewStyle>
}
