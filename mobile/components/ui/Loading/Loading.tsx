import { ActivityIndicator, View } from 'react-native'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './Loading.styles'
import type { LoadingProps, LoadingSize } from './Loading.types'
export type { LoadingProps, LoadingSize }

const SIZE_MAP: Record<string, number> = {
	sm: 18,
	md: 24,
	lg: 32,
}

export function Loading({ size = 'md', color, style }: LoadingProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])

	const indicatorColor = color ?? theme.palette.primary[500]
	const nativeSize = SIZE_MAP[size] ?? 24

	return (
		<View style={[styles.container, style]}>
			<ActivityIndicator size={nativeSize} color={indicatorColor} />
		</View>
	)
}
