import { Pressable, View } from 'react-native'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './Card.styles'
import type { CardProps } from './Card.types'

const PADDING_MAP: Record<number, number> = {
	0: 0, 1: 4, 2: 8, 3: 12, 4: 16, 5: 20, 6: 24, 8: 32, 10: 40, 12: 48, 16: 64,
}

export function Card({
	variant = 'elevated',
	padding = 4,
	onPress,
	children,
	style,
}: CardProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])

	const paddingValue = PADDING_MAP[padding] ?? 16

	const content = (
		<View style={[styles.base as object, styles[`variant_${variant}`] as object, { padding: paddingValue }, style as object]}>
			{children}
		</View>
	)

	if (onPress) {
		return (
			<Pressable onPress={onPress} style={({ pressed }) => [pressed ? { opacity: 0.8 } : null]}>
				{content}
			</Pressable>
		)
	}

	return content
}
