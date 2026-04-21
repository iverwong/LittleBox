import type { StyleProp, ViewStyle } from 'react-native'

export type LoadingSize = 'sm' | 'md' | 'lg'

export interface LoadingProps {
	size?: LoadingSize
	color?: string
	style?: StyleProp<ViewStyle>
}
