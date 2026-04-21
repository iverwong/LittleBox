import { Text } from 'react-native'
import { Feather } from '@expo/vector-icons'
import { useEffect } from 'react'
import Animated, {
	useSharedValue,
	useAnimatedStyle,
	withTiming,
	runOnJS,
} from 'react-native-reanimated'
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

export function Toast({ message, variant, onDismiss, style }: ToastProps) {
	const theme = useTheme()
	const styles = createStyles(theme)
	const opacity = useSharedValue(0)
	const translateY = useSharedValue(-20)

	useEffect(() => {
		opacity.value = withTiming(1, { duration: 200 })
		translateY.value = withTiming(0, { duration: 200 })
	}, [opacity, translateY])

	const animatedStyle = useAnimatedStyle(() => ({
		opacity: opacity.value,
		transform: [{ translateY: translateY.value }],
	}))

	useEffect(() => {
		const timer = setTimeout(() => {
			opacity.value = withTiming(0, { duration: 200 }, (finished) => {
				if (finished) {
					runOnJS(onDismiss)()
				}
			})
			translateY.value = withTiming(-20, { duration: 200 })
		}, 3000)
		return () => clearTimeout(timer)
	}, [opacity, translateY, onDismiss])

	const bgColor = VARIANT_COLORS[variant]

	return (
		<Animated.View
			style={[
				styles.container,
				{ backgroundColor: bgColor },
				animatedStyle,
				style as object,
			]}
		>
			<Feather name={ICON_MAP[variant]} size={18} color="#FFFFFF" style={styles.icon as object} />
			<Text style={styles.message}>{message}</Text>
		</Animated.View>
	)
}
