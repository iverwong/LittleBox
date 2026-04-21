import { StyleSheet } from 'react-native'
import type { TextStyle, ViewStyle } from 'react-native'
import type { Theme } from '@/theme'

type InputStyles = {
	wrap: ViewStyle
	container: ViewStyle
	input: TextStyle
	size_md: TextStyle
	size_lg: TextStyle
	iconLeft: ViewStyle
	iconRight: ViewStyle
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
			borderWidth: 1,
			borderColor: theme.palette.neutral[200],
			borderRadius: theme.radius.md,
			paddingHorizontal: theme.spacing[3],
		},
		input: { flex: 1, color: theme.palette.neutral[800] },
		size_md: sizeStyles.md,
		size_lg: sizeStyles.lg,
		iconLeft: { marginRight: theme.spacing[2] },
		iconRight: { marginLeft: theme.spacing[2] },
		errorText: { fontSize: theme.typography.fontSize.xs, color: theme.ui.error },
	})
}
