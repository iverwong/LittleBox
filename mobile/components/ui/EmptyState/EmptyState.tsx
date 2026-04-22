import { View, Text, Image } from 'react-native'
import { Feather } from '@expo/vector-icons'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './EmptyState.styles'
import type { EmptyStateProps } from './EmptyState.types'
import type { FeatherName } from '../types'
export type { EmptyStateProps }

export function EmptyState({ illustration, icon, title, description, action }: EmptyStateProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])

	return (
		<View style={styles.container}>
			{(illustration || icon) && (
				<View style={styles.iconWrap}>
					{illustration ? (
						<Image source={illustration} resizeMode="contain" style={{ width: 120, height: 120 }} />
					) : (
						icon && <Feather name={icon as FeatherName} size={64} color={theme.palette.neutral[400]} />
					)}
				</View>
			)}
			<Text style={styles.title}>{title}</Text>
			{description && <Text style={styles.description}>{description}</Text>}
			{action && <View style={{ marginTop: theme.spacing[2] }}>{action}</View>}
		</View>
	)
}
