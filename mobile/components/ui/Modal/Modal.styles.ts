import { StyleSheet } from 'react-native'
import type { TextStyle, ViewStyle } from 'react-native'
import type { Theme } from '@/theme'

type ModalStyles = {
	backdrop: ViewStyle
	panel: ViewStyle
	size_sm: ViewStyle
	size_md: ViewStyle
	size_lg: ViewStyle
	size_full: ViewStyle
	header: ViewStyle
	title: TextStyle
	closeBtn: ViewStyle
	content: ViewStyle
	footer: ViewStyle
}

export const createStyles = (_theme: Theme): ModalStyles => {
	return StyleSheet.create({
		backdrop: {
			flex: 1,
			backgroundColor: 'rgba(0, 0, 0, 0.4)',
			justifyContent: 'flex-end',
			alignItems: 'center',
		},
		panel: {
			backgroundColor: _theme.surface.paper,
			borderTopLeftRadius: _theme.radius['2xl'],
			borderTopRightRadius: _theme.radius['2xl'],
			overflow: 'hidden',
		},
		size_sm: {
			width: '70%',
			maxHeight: '40%',
		},
		size_md: {
			width: '85%',
			maxHeight: '55%',
		},
		size_lg: {
			width: '95%',
			maxHeight: '75%',
		},
		size_full: {
			width: '100%',
			maxHeight: '100%',
			borderTopLeftRadius: 0,
			borderTopRightRadius: 0,
		},
		header: {
			flexDirection: 'row',
			alignItems: 'center',
			justifyContent: 'space-between',
			paddingVertical: _theme.spacing[4],
			paddingHorizontal: _theme.spacing[5],
			borderBottomWidth: 1,
			borderBottomColor: _theme.palette.neutral[100],
		},
		title: {
			fontSize: _theme.typography.fontSize.lg,
			fontWeight: _theme.typography.fontWeight.semibold,
			color: _theme.palette.neutral[800],
			flex: 1,
		},
		closeBtn: {
			padding: _theme.spacing[1],
			marginLeft: _theme.spacing[2],
		},
		content: {
			paddingVertical: _theme.spacing[4],
			paddingHorizontal: _theme.spacing[5],
		},
		footer: {
			paddingVertical: _theme.spacing[4],
			paddingHorizontal: _theme.spacing[5],
			borderTopWidth: 1,
			borderTopColor: _theme.palette.neutral[100],
		},
	})
}
