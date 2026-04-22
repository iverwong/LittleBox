import { StyleSheet } from 'react-native'
import type { ViewStyle } from 'react-native'
import type { Theme } from '@/theme'

type ScreenContainerStyles = {
	container: ViewStyle
	contentContainer: ViewStyle
}

export const createStyles = (_theme: Theme): ScreenContainerStyles => {
	return StyleSheet.create({
		container: {
			flex: 1,
		},
		contentContainer: {
			flexGrow: 1,
		},
	})
}
