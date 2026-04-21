import { Image, Text, View } from 'react-native'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './Avatar.styles'
import type { AvatarProps } from './Avatar.types'

const FONT_SIZE = { sm: 16, md: 22, lg: 28, xl: 36 } as const

export function Avatar({
	source,
	name,
	size = 'md',
	badge,
	backgroundColor,
	style,
}: AvatarProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])
	const bg = backgroundColor ?? theme.palette.primary[200]
	const initial = name?.trim().charAt(0).toUpperCase() ?? ''

	return (
		<View style={[styles.wrap as object, styles[`size_${size}`] as object, { backgroundColor: bg }, style as object]}>
			{source ? (
				<Image source={source} style={[styles.wrap as object, styles[`size_${size}`] as object]} />
			) : (
				initial ? (
					<Text style={{ fontSize: FONT_SIZE[size], fontWeight: theme.typography.fontWeight.semibold, color: theme.palette.primary[700] }}>
						{initial}
					</Text>
				) : null
			)}
			{badge ? <View style={styles.badge as object}>{badge}</View> : null}
		</View>
	)
}
