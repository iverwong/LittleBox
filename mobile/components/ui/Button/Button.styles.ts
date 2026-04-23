import { StyleSheet } from 'react-native'
import type { ViewStyle, TextStyle } from 'react-native'
import type { Theme } from '@/theme'

type ButtonStyles = {
	base: ViewStyle
	variant_primary: ViewStyle
	variant_secondary: ViewStyle
	variant_ghost: ViewStyle
	variant_danger: ViewStyle
	size_sm: ViewStyle
	size_md: ViewStyle
	size_lg: ViewStyle
	pressed: ViewStyle
	disabled: ViewStyle
	icon: TextStyle
	spinner: TextStyle
}

export const createStyles = (theme: Theme): ButtonStyles => {
	return StyleSheet.create({
		base: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', borderRadius: theme.radius.full, gap: theme.spacing[2] },
		variant_primary: { backgroundColor: theme.palette.primary[500], borderWidth: 0 },
		variant_secondary: { backgroundColor: theme.palette.secondary[500], borderWidth: 0 },
		variant_ghost: { backgroundColor: 'transparent', borderWidth: 1, borderColor: theme.palette.secondary[400] },
		variant_danger: { backgroundColor: theme.ui.error, borderWidth: 0 },
		size_sm: { height: 36, paddingHorizontal: theme.spacing[3], minWidth: 60 },
		size_md: { height: 44, paddingHorizontal: theme.spacing[4], minWidth: 80 },
		size_lg: { height: 52, paddingHorizontal: theme.spacing[5], minWidth: 100 },
		pressed: { transform: [{ scale: 0.97 }], opacity: 0.9 },
		disabled: { opacity: 0.5 },
		icon: { marginRight: 4 },
		spinner: { marginRight: 4 },
	})
}
