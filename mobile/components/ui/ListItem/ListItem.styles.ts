import { StyleSheet } from 'react-native'
import type { ViewStyle, TextStyle } from 'react-native'
import type { Theme } from '@/theme'

type ListItemStyles = {
	container: ViewStyle
	divider: ViewStyle
	content: ViewStyle
	leading: ViewStyle
	textWrap: ViewStyle
	title: TextStyle
	subtitle: TextStyle
	trailing: ViewStyle
}

export const createStyles = (theme: Theme): ListItemStyles => {
	return StyleSheet.create({
		container: {
			paddingVertical: theme.spacing[3],
			paddingHorizontal: theme.spacing[4],
		},
		divider: {
			height: 1,
			backgroundColor: theme.palette.neutral[100],
			marginLeft: theme.spacing[4],
		},
		content: {
			flex: 1,
			flexDirection: 'row',
			alignItems: 'center',
		},
		leading: {
			marginRight: theme.spacing[3],
		},
		textWrap: {
			flex: 1,
		},
		title: {
			fontSize: theme.typography.fontSize.base,
			fontWeight: theme.typography.fontWeight.medium,
			color: theme.palette.neutral[800],
		},
		subtitle: {
			fontSize: theme.typography.fontSize.sm,
			fontWeight: theme.typography.fontWeight.regular,
			color: theme.palette.neutral[500],
			marginTop: 2,
		},
		trailing: {
			marginLeft: theme.spacing[3],
		},
	})
}
