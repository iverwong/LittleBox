import { StyleSheet } from 'react-native'
import type { Theme } from '@/theme'
import type { ModalSize } from './Modal.types'

const SIZE_WIDTH: Record<ModalSize, string | number> = {
	sm: 320,
	md: 420,
	lg: 560,
	full: '92%',
}

export const createStyles = (_theme: Theme) => {
	return StyleSheet.create({
		backdrop: {
			flex: 1,
			backgroundColor: 'rgba(0, 0, 0, 0.4)',
			justifyContent: 'flex-end',
			alignItems: 'center',
		},
		panel: {
			backgroundColor: '#FFFFFF',
			borderTopLeftRadius: _theme.radius['2xl'],
			borderTopRightRadius: _theme.radius['2xl'],
			width: '100%',
			maxWidth: SIZE_WIDTH.lg as number,
			maxHeight: '90%',
			overflow: 'hidden',
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
