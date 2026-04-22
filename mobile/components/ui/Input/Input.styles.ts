import { StyleSheet } from 'react-native'
import type { TextStyle, ViewStyle } from 'react-native'
import type { Theme } from '@/theme'

type InputStyles = {
	wrap: ViewStyle
	container: ViewStyle
	container_focused: ViewStyle
	container_error: ViewStyle
	container_disabled: ViewStyle
	input: TextStyle
	size_md: TextStyle
	size_lg: TextStyle
	iconLeft: TextStyle
	iconRight: TextStyle
	errorText: TextStyle
}

export const createStyles = (theme: Theme): InputStyles => {
	const sizeStyles: Record<'md' | 'lg', TextStyle> = {
		md: { fontSize: theme.typography.fontSize.base, paddingVertical: theme.spacing[2] },
		lg: { fontSize: theme.typography.fontSize.md, paddingVertical: theme.spacing[3] },
	}

	return StyleSheet.create({
		wrap: { gap: theme.spacing[1] },
		container: {
			flexDirection: 'row',
			alignItems: 'center',
			backgroundColor: theme.palette.neutral[50],
			borderWidth: 2,
			borderColor: theme.palette.neutral[200],
			borderRadius: theme.radius.md,
			paddingHorizontal: theme.spacing[3],
		},
		container_focused: {
			borderColor: theme.palette.primary[400],
			shadowColor: theme.palette.primary[500],
			shadowOpacity: 0.25,
			shadowRadius: 4,
			shadowOffset: { width: 0, height: 0 },
			elevation: 3,
		},
		container_error: {
			borderColor: theme.ui.error,
			borderWidth: 2,
		},
		container_disabled: {
			backgroundColor: theme.palette.neutral[100],
			borderColor: theme.palette.neutral[200],
		},
		input: { flex: 1, color: theme.palette.neutral[800] },
		size_md: sizeStyles.md,
		size_lg: sizeStyles.lg,
		iconLeft: { marginRight: theme.spacing[2] },
		iconRight: { marginLeft: theme.spacing[2] },
		errorText: { fontSize: theme.typography.fontSize.xs, color: theme.ui.error },
	})
}
