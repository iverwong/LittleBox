import { Image, Text, View } from 'react-native'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './Avatar.styles'
import type { AvatarProps } from './Avatar.types'

const FONT_SIZE = { sm: 16, md: 22, lg: 28, xl: 36 } as const
const AVATAR_SIZE: Record<'sm' | 'md' | 'lg' | 'xl', number> = { sm: 40, md: 56, lg: 72, xl: 96 }

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
		<View style={[styles.wrap, styles[`size_${size}`], { backgroundColor: bg }, style]}>
			{source ? (
				<Image source={source} style={{ width: AVATAR_SIZE[size], height: AVATAR_SIZE[size], borderRadius: theme.radius.full }} />
			) : (
				initial ? (
					<Text style={{ fontSize: FONT_SIZE[size], fontWeight: theme.typography.fontWeight.semibold, color: theme.palette.primary[700] }}>
						{initial}
					</Text>
				) : null
			)}
			{badge ? <View style={styles.badge}>{badge}</View> : null}
		</View>
	)
}
