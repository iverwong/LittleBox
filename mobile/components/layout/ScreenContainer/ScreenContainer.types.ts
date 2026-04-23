import type { ReactNode } from 'react'
import type { StyleProp, ViewStyle, ScrollViewProps } from 'react-native'

export interface ScreenContainerProps {
	children: ReactNode
	background?: 'primary' | 'secondary' | 'neutral'
	scrollable?: boolean
	ScrollViewProps?: ScrollViewProps
	style?: StyleProp<ViewStyle>
}
