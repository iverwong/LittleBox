import { StyleSheet } from 'react-native'
import type { ViewStyle } from 'react-native'
import type { Theme } from '@/theme'

type CardStyles = Record<string, ViewStyle>

export const createStyles = (theme: Theme): CardStyles => {
	return StyleSheet.create({
		base: { borderRadius: theme.radius.lg, overflow: 'hidden' },
		variant_elevated: { backgroundColor: theme.palette.neutral[50], ...theme.shadow.md },
		variant_outlined: { backgroundColor: theme.palette.neutral[50], borderWidth: 1, borderColor: theme.palette.neutral[200] },
		variant_filled: { backgroundColor: theme.palette.neutral[100] },
		variant_accentSecondary: { backgroundColor: theme.palette.secondary[50], borderLeftWidth: 3, borderLeftColor: theme.palette.secondary[500] },
	})
}
