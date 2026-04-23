import { StyleSheet } from 'react-native'
import type { TextStyle, ViewStyle } from 'react-native'
import type { Theme } from '@/theme'

type HeaderStyles = {
	container: ViewStyle
	row: ViewStyle
	leading: ViewStyle
	titleWrap: ViewStyle
	title: TextStyle
	subtitle: TextStyle
	trailing: ViewStyle
}

export const createStyles = (_theme: Theme): HeaderStyles => {
	return StyleSheet.create({
		container: {
			width: '100%',
			backgroundColor: _theme.palette.neutral[50],
			borderBottomWidth: 1,
			borderBottomColor: _theme.palette.neutral[100],
		},
		row: {
			flexDirection: 'row',
			alignItems: 'center',
			minHeight: 56,
			paddingHorizontal: _theme.spacing[4],
		},
		leading: {
			marginRight: _theme.spacing[3],
		},
		titleWrap: {
			flex: 1,
		},
		title: {
			fontSize: _theme.typography.fontSize.lg,
			fontWeight: _theme.typography.fontWeight.semibold,
			color: _theme.palette.neutral[800],
		},
		subtitle: {
			fontSize: _theme.typography.fontSize.sm,
			color: _theme.palette.neutral[500],
			marginTop: 2,
		},
		trailing: {
			marginLeft: _theme.spacing[3],
		},
	})
}
