import { useEffect } from 'react'
import { View } from 'react-native'
import {
	useSharedValue, useAnimatedProps,
	withRepeat, withSequence, withTiming,
} from 'react-native-reanimated'
import type { MascotProps, MascotSize } from './Mascot.types'
import { MascotSvg } from './MascotSvg'
import {
	BLINK_PERIOD_MS, BLINK_DURATION_MS, EYE_CENTER_Y,
} from './animations'

const SIZE_PX: Record<MascotSize, number> = { sm: 48, md: 96, lg: 128, xl: 200 }

/**
 * Mascot — MVP: static SVG body + single blink animation.
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

	useEffect(() => {
		// Continuous 4s blink cycle: hold open → squeeze → hold open
		blinkScaleY.value = withRepeat(
			withSequence(
				withTiming(1, { duration: BLINK_PERIOD_MS - BLINK_DURATION_MS }),
				withTiming(0.1, { duration: BLINK_DURATION_MS / 2 }),
				withTiming(1, { duration: BLINK_DURATION_MS / 2 }),
			),
			-1,  // infinite
			false,
		)
	}, [blinkScaleY])

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
