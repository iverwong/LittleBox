import { StyleSheet } from 'react-native'
import type { ViewStyle } from 'react-native'
import type { Theme } from '@/theme'

type LoadingStyles = {
	container: ViewStyle
}

export const createStyles = (theme: Theme): LoadingStyles => {
	return StyleSheet.create({
		container: {
			alignItems: 'center',
			justifyContent: 'center',
		},
	})
}
