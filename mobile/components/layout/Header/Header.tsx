import { View, Text } from 'react-native'
import { useSafeAreaInsets } from 'react-native-safe-area-context'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './Header.styles'
import type { HeaderProps } from './Header.types'
export type { HeaderProps }

export function Header({ title, subtitle, leading, trailing, safeArea = true }: HeaderProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])
	const insets = useSafeAreaInsets()

	const topPadding = safeArea ? insets.top : 0

	return (
		<View style={[styles.container, { paddingTop: topPadding }]}>
			<View style={styles.row}>
				{leading && <View style={styles.leading}>{leading}</View>}
				<View style={styles.titleWrap}>
					{title && <Text style={styles.title}>{title}</Text>}
					{subtitle && <Text style={styles.subtitle}>{subtitle}</Text>}
				</View>
				{trailing && <View style={styles.trailing}>{trailing}</View>}
			</View>
		</View>
	)
}
