import { Modal as RNModal, View, Text, Pressable } from 'react-native'
import { Feather } from '@expo/vector-icons'
import { useMemo, useEffect } from 'react'
import { useSafeAreaInsets } from 'react-native-safe-area-context'
import Animated, {
	useSharedValue,
	useAnimatedStyle,
	withSpring,
	withTiming,
} from 'react-native-reanimated'
import { useTheme } from '@/theme'
import { createStyles } from './Modal.styles'
import type { ModalProps } from './Modal.types'
export type { ModalProps } from './Modal.types'
export type { ModalSize } from './Modal.types'

const AnimatedPressable = Animated.createAnimatedComponent(Pressable)

const OPEN_SPRING = { damping: 20, stiffness: 240, mass: 0.6 }
const CLOSE_SPRING = { damping: 20, stiffness: 200, mass: 0.6 }

export function Modal({
	visible,
	onClose,
	title,
	children,
	footer,
	size = 'md',
	dismissOnBackdrop = true,
	style,
}: ModalProps) {
	const theme = useTheme()
	const insets = useSafeAreaInsets()
	const styles = useMemo(() => createStyles(theme), [theme])

	const translateY = useSharedValue(300)
	const opacity = useSharedValue(0)

	const animatedStyle = useAnimatedStyle(() => ({
		transform: [{ translateY: translateY.value }],
	}))

	const backdropStyle = useAnimatedStyle(() => ({
		opacity: opacity.value,
	}))

	// Animate on visibility changes — must be in useEffect, not render body
	useEffect(() => {
		if (visible) {
			translateY.value = withSpring(0, OPEN_SPRING)
			opacity.value = withTiming(1, { duration: 200 })
		} else {
			translateY.value = withSpring(300, CLOSE_SPRING)
			opacity.value = withTiming(0, { duration: 150 })
		}
	}, [visible, translateY, opacity])

	const handleBackdropPress = () => {
		if (dismissOnBackdrop) {
			onClose()
		}
	}

	const panelPaddingBottom =
		size === 'full' ? insets.bottom : insets.bottom + theme.spacing[4]

	return (
		<RNModal
			transparent
			visible={visible}
			animationType="none"
			onRequestClose={onClose}
			statusBarTranslucent
		>
			<AnimatedPressable
				style={[styles.backdrop, backdropStyle]}
				onPress={handleBackdropPress}
			>
				<Animated.View
					style={[
						styles.panel,
						styles[`size_${size}`],
						{ paddingBottom: panelPaddingBottom },
						style,
						animatedStyle,
					]}
					onStartShouldSetResponder={() => true}
					onResponderRelease={() => {}}
				>
					{title && (
						<View style={styles.header}>
							<Text style={styles.title}>{title}</Text>
							<Pressable onPress={onClose} style={styles.closeBtn} hitSlop={8}>
								<Feather name="x" size={20} color={theme.palette.neutral[600]} />
							</Pressable>
						</View>
					)}
					<View style={styles.content}>{children}</View>
					{footer && <View style={styles.footer}>{footer}</View>}
				</Animated.View>
			</AnimatedPressable>
		</RNModal>
	)
}
