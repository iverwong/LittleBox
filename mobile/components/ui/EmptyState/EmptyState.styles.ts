import { StyleSheet } from 'react-native'
import type { ViewStyle, TextStyle } from 'react-native'
import type { Theme } from '@/theme'

type EmptyStateStyles = {
	container: ViewStyle
	iconWrap: ViewStyle
	title: TextStyle
	description: TextStyle
}

export const createStyles = (theme: Theme): EmptyStateStyles => {
	return StyleSheet.create({
		container: {
			flex: 1,
			alignItems: 'center',
			justifyContent: 'center',
			padding: theme.spacing[8],
			gap: theme.spacing[3],
		},
		iconWrap: {
			marginBottom: theme.spacing[2],
		},
		title: {
			fontSize: theme.typography.fontSize.lg,
			fontWeight: theme.typography.fontWeight.semibold,
			color: theme.palette.neutral[800],
			textAlign: 'center',
		},
		description: {
			fontSize: theme.typography.fontSize.base,
			fontWeight: theme.typography.fontWeight.regular,
			color: theme.palette.neutral[500],
			textAlign: 'center',
			lineHeight: theme.typography.fontSize.base * theme.typography.lineHeight.normal,
		},
	})
}
