import { Modal as RNModal, View, Text, Pressable } from 'react-native'
import { Feather } from '@expo/vector-icons'
import { useMemo, useEffect } from 'react'
import Animated, {
	useSharedValue,
	useAnimatedStyle,
	withSpring,
	withTiming,
} from 'react-native-reanimated'
import { useTheme } from '@/theme'
import { createStyles } from './Modal.styles'
import type { ModalProps } from './Modal.types'
export type { ModalProps, ModalSize } from './Modal.types'

const AnimatedPressable = Animated.createAnimatedComponent(Pressable)

const OPEN_SPRING = { damping: 20, stiffness: 200 }
const CLOSE_SPRING = { damping: 20, stiffness: 150 }

export function Modal({
	visible,
	onClose,
	title,
	children,
	footer,
	dismissOnBackdrop = true,
	style,
}: ModalProps) {
	const theme = useTheme()
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

	return (
		<RNModal
			transparent
			visible={visible}
			animationType="none"
			onRequestClose={onClose}
			statusBarTranslucent
		>
			<AnimatedPressable
				style={[styles.backdrop as object, backdropStyle]}
				onPress={handleBackdropPress}
			>
				<Animated.View
					style={[styles.panel, style as object, animatedStyle]}
					onStartShouldSetResponder={() => true}
					onResponderRelease={() => {}}
				>
					{title && (
						<View style={styles.header as object}>
							<Text style={styles.title as object}>{title}</Text>
							<Pressable onPress={onClose} style={styles.closeBtn as object} hitSlop={8}>
								<Feather name="x" size={20} color={theme.palette.neutral[600]} />
							</Pressable>
						</View>
					)}
					<View style={styles.content as object}>{children}</View>
					{footer && <View style={styles.footer as object}>{footer}</View>}
				</Animated.View>
			</AnimatedPressable>
		</RNModal>
	)
}
