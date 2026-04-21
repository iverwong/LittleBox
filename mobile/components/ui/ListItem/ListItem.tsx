import { Pressable, View, Text } from 'react-native'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './ListItem.styles'
import type { ListItemProps } from './ListItem.types'
export type { ListItemProps }

export function ListItem({ leading, title, subtitle, trailing, onPress, divider = false }: ListItemProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])

	const content = (
		<View style={styles.content as object}>
			{leading && <View style={styles.leading as object}>{leading}</View>}
			<View style={styles.textWrap as object}>
				<Text style={styles.title}>{title}</Text>
				{subtitle && <Text style={styles.subtitle as object}>{subtitle}</Text>}
			</View>
			{trailing && <View style={styles.trailing as object}>{trailing}</View>}
		</View>
	)

	if (onPress) {
		return (
			<Pressable onPress={onPress} style={({ pressed }) => [pressed ? { opacity: 0.7 } : null]}>
				{content}
				{divider && <View style={styles.divider as object} />}
			</Pressable>
		)
	}

	return (
		<View>
			{content}
			{divider && <View style={styles.divider as object} />}
		</View>
	)
}
