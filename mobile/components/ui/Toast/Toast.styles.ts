import { StyleSheet } from 'react-native'
import type { ViewStyle, TextStyle } from 'react-native'
import type { Theme } from '@/theme'
import type { ToastVariant } from './toastStore'

type ToastStyles = {
	container: ViewStyle
	icon: TextStyle
	message: TextStyle
}

const VARIANT_COLORS: Record<ToastVariant, string> = {
	info: '#6A8CA8',
	success: '#7A9180',
	warning: '#D89155',
	error: '#C56B5E',
}

export const createStyles = (_theme: Theme): ToastStyles => {
	return StyleSheet.create({
		container: {
			flexDirection: 'row',
			alignItems: 'center',
			paddingVertical: _theme.spacing[3],
			paddingHorizontal: _theme.spacing[4],
			borderRadius: _theme.radius.md,
			marginHorizontal: _theme.spacing[4],
			gap: _theme.spacing[2],
			shadowColor: '#3C2814',
			shadowOpacity: 0.1,
			shadowOffset: { width: 0, height: 2 },
			shadowRadius: 6,
			elevation: 4,
		},
		icon: {
			width: 20,
			height: 20,
		},
		message: {
			flex: 1,
			fontSize: _theme.typography.fontSize.sm,
			fontWeight: _theme.typography.fontWeight.medium,
			color: '#FFFFFF',
		},
	})
}

export { VARIANT_COLORS }
