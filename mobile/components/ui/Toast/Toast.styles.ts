import { StyleSheet } from 'react-native'
import type { ViewStyle, TextStyle } from 'react-native'
import type { Theme } from '@/theme'
import type { ToastVariant } from './toastStore'

// eslint-disable-next-line @typescript-eslint/no-unused-vars
type _ToastStyles = Record<string, ViewStyle | TextStyle>

const VARIANT_COLORS: Record<ToastVariant, string> = {
	info: '#6A8CA8',
	success: '#7A9180',
	warning: '#D89155',
	error: '#C56B5E',
}

export const createStyles = (theme: Theme) => {
	return StyleSheet.create({
		container: {
			flexDirection: 'row',
			alignItems: 'center',
			paddingVertical: theme.spacing[3],
			paddingHorizontal: theme.spacing[4],
			borderRadius: theme.radius.md,
			marginHorizontal: theme.spacing[4],
			gap: theme.spacing[2],
			// Shadow for elevation feel
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
			fontSize: theme.typography.fontSize.sm,
			fontWeight: theme.typography.fontWeight.medium,
			color: '#FFFFFF',
		},
	})
}

export { VARIANT_COLORS }
