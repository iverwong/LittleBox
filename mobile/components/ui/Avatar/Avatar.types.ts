import type { ImageSourcePropType, StyleProp, ViewStyle } from 'react-native'

export type AvatarSize = 'sm' | 'md' | 'lg' | 'xl'

export interface AvatarProps {
	source?: ImageSourcePropType
	name?: string
	size?: AvatarSize
	badge?: React.ReactNode
	backgroundColor?: string
	style?: StyleProp<ViewStyle>
}
