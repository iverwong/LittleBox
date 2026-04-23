import { StyleSheet } from 'react-native'
import type { ViewStyle } from 'react-native'
import type { Theme } from '@/theme'

type AvatarStyles = {
	wrap: ViewStyle
	size_sm: ViewStyle
	size_md: ViewStyle
	size_lg: ViewStyle
	size_xl: ViewStyle
	badge: ViewStyle
}

export const createStyles = (theme: Theme): AvatarStyles => {
	return StyleSheet.create({
		wrap: { borderRadius: theme.radius.full, overflow: 'visible', alignItems: 'center', justifyContent: 'center' },
		size_sm: { width: 40, height: 40 },
		size_md: { width: 56, height: 56 },
		size_lg: { width: 72, height: 72 },
		size_xl: { width: 96, height: 96 },
		badge: { position: 'absolute', bottom: 0, right: 0 },
	})
}
