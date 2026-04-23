import type { ReactNode } from 'react'
import type { StyleProp, ViewStyle } from 'react-native'

export type ModalSize = 'sm' | 'md' | 'lg' | 'full'

export interface ModalProps {
	visible: boolean
	onClose: () => void
	title?: string
	children: ReactNode
	footer?: ReactNode
	dismissOnBackdrop?: boolean
	size?: ModalSize
	style?: StyleProp<ViewStyle>
}
