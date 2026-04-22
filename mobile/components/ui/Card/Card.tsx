import { Pressable, View } from 'react-native'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './Card.styles'
import type { CardProps } from './Card.types'

export function Card({
	variant = 'elevated',
	padding = 4,
	onPress,
	children,
	style,
}: CardProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])

	const paddingValue = theme.spacing[padding] ?? 16

	const content = (
		<View style={[styles.base, styles[`variant_${variant}`], { padding: paddingValue }, style]}>
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
