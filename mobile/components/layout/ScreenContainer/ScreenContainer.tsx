import { View, ScrollView } from 'react-native'
import { useSafeAreaInsets } from 'react-native-safe-area-context'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './ScreenContainer.styles'
import type { ScreenContainerProps } from './ScreenContainer.types'
export type { ScreenContainerProps }

export function ScreenContainer({
	children,
	background,
	scrollable = false,
	ScrollViewProps: _scrollViewProps,
	style,
}: ScreenContainerProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])
	const insets = useSafeAreaInsets()

	const bgColor = background
		? theme.palette[background][50]
		: theme.palette.neutral[50]

	const containerStyle = {
		backgroundColor: bgColor,
		paddingTop: insets.top,
		paddingBottom: insets.bottom,
		paddingLeft: insets.left,
		paddingRight: insets.right,
	}

	if (scrollable) {
		return (
			<ScrollView
				style={[styles.container, containerStyle, style]}
				contentContainerStyle={styles.contentContainer}
			>
				{children}
			</ScrollView>
		)
	}

	return (
		<View style={[styles.container, containerStyle, style]}>
			{children}
		</View>
	)
}
