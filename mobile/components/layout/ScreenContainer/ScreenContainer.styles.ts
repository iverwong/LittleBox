import { StyleSheet } from 'react-native'
import type { Theme } from '@/theme'

type ScreenContainerStyles = Record<string, object>

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
