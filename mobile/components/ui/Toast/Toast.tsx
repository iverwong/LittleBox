import { Text } from 'react-native'
import { Feather } from '@expo/vector-icons'
import Animated from 'react-native-reanimated'
import { useTheme } from '@/theme'
import { createStyles, VARIANT_COLORS } from './Toast.styles'
import type { ToastProps } from './Toast.types'
import type { FeatherName } from '../types'

const ICON_MAP: Record<string, FeatherName> = {
	info: 'info',
	success: 'check-circle',
	warning: 'alert-triangle',
	error: 'x-circle',
}

// Toast is rendered inside Animated.View in ToastContainer.
// Entry/exit animations are handled by the parent Animated.View (FadeInDown/FadeOut).
// No internal animation needed here.
export function Toast({ message, variant, style }: ToastProps) {
	const theme = useTheme()
	const styles = createStyles(theme)
	const bgColor = VARIANT_COLORS[variant]

	return (
		<Animated.View
			style={[
				styles.container,
				{ backgroundColor: bgColor },
				style,
			]}
		>
			<Feather name={ICON_MAP[variant]} size={18} color="#FFFFFF" style={styles.icon} />
			<Text style={styles.message}>{message}</Text>
		</Animated.View>
	)
}
