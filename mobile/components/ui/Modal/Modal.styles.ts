import { StyleSheet, DimensionValue } from 'react-native'
import type { Theme } from '@/theme'

type ModalStyles = Record<string, object>

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
			width: '100%' as DimensionValue,
			maxWidth: 320,
			maxHeight: '40%',
			marginBottom: _theme.spacing[4],
		},
		size_md: {
			width: '100%' as DimensionValue,
			maxWidth: 420,
			maxHeight: '55%',
			marginBottom: _theme.spacing[4],
		},
		size_lg: {
			width: '100%' as DimensionValue,
			maxWidth: 520,
			maxHeight: '75%',
			marginBottom: _theme.spacing[4],
		},
		size_full: {
			width: '100%' as DimensionValue,
			maxWidth: '100%' as DimensionValue,
			maxHeight: '90%',
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
