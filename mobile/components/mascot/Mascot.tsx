import { useEffect, useRef } from 'react'
import { View } from 'react-native'
import {
	useSharedValue, useAnimatedProps,
	withSequence, withTiming, cancelAnimation,
} from 'react-native-reanimated'
import type { MascotProps, MascotSize } from './Mascot.types'
import { MascotSvg } from './MascotSvg'
import {
	BLINK_INTERVAL_MIN_MS, BLINK_INTERVAL_MAX_MS,
	BLINK_DOUBLE_PROB, BLINK_CLOSE_MS, BLINK_OPEN_MS,
	BLINK_DOUBLE_GAP_MS, EYE_CENTER_Y,
} from './animations'

const SIZE_PX: Record<MascotSize, number> = { sm: 48, md: 96, lg: 128, xl: 200 }

/**
 * Mascot — MVP: static SVG body + randomized blink animation.
 *
 * Props (M4.5 contract) are preserved for backward compatibility:
 * - `state` is accepted but has no runtime effect.
 * - `onFinish` is accepted but never called.
 * - `size` and `style` work as expected.
 */
export function Mascot({ state: _state = 'idle', size = 'md', onFinish: _onFinish, style }: MascotProps) {
	const px = SIZE_PX[size]

	// Single shared value: blink scaleY (1 = open, 0.1 = closed)
	const blinkScaleY = useSharedValue(1)

	// Timer ref for JS scheduler cleanup
	const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

	useEffect(() => {
		// Cancel any running blink animation before starting fresh
		cancelAnimation(blinkScaleY)
		blinkScaleY.value = 1

		/**
		 * Schedule the next blink after a random interval.
		 * Runs in JS (not a worklet) so Math.random() is safe.
		 */
		function scheduleNextBlink() {
			const interval = Math.random() * (BLINK_INTERVAL_MAX_MS - BLINK_INTERVAL_MIN_MS) + BLINK_INTERVAL_MIN_MS
			timerRef.current = setTimeout(() => {
				// Decide single (75%) vs double (25%) blink
				const isDouble = Math.random() < BLINK_DOUBLE_PROB

				if (isDouble) {
					// Double blink: close → open(gap) → close → open
					blinkScaleY.value = withSequence(
						withTiming(0.1, { duration: BLINK_CLOSE_MS }),
						withTiming(1,   { duration: BLINK_DOUBLE_GAP_MS }),
						withTiming(0.1, { duration: BLINK_CLOSE_MS }),
						withTiming(1,   { duration: BLINK_OPEN_MS }),
					)
				} else {
					// Single blink: close → open
					blinkScaleY.value = withSequence(
						withTiming(0.1, { duration: BLINK_CLOSE_MS }),
						withTiming(1,   { duration: BLINK_OPEN_MS }),
					)
				}

				// Schedule the next one after this blink sequence finishes
				const sequenceMs = isDouble
					? BLINK_CLOSE_MS + BLINK_DOUBLE_GAP_MS + BLINK_CLOSE_MS + BLINK_OPEN_MS
					: BLINK_CLOSE_MS + BLINK_OPEN_MS

				timerRef.current = setTimeout(scheduleNextBlink, sequenceMs)
			}, interval)
		}

		scheduleNextBlink()

		// Cleanup: cancel pending timer and any running animation on unmount
		return () => {
			if (timerRef.current !== null) {
				clearTimeout(timerRef.current)
				timerRef.current = null
			}
			cancelAnimation(blinkScaleY)
		}
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [])  // intentionally empty: blink scheduler is a self-contained loop, no deps needed

	const blinkAnimatedProps = useAnimatedProps(() => ({
		transform: [
			{ translateY: EYE_CENTER_Y },
			{ scaleY: blinkScaleY.value },
			{ translateY: -EYE_CENTER_Y },
		],
	}))

	return (
		<View style={[{ width: px, height: px }, style]}>
			<MascotSvg blinkAnimatedProps={blinkAnimatedProps} />
		</View>
	)
}
