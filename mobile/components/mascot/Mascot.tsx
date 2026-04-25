import { useEffect } from 'react'
import { View } from 'react-native'
import {
	useSharedValue, useAnimatedProps,
	withTiming, withRepeat, withSequence, withDelay,
	cancelAnimation, runOnJS,
} from 'react-native-reanimated'
import type { MascotProps, MascotSize } from './Mascot.types'
import { MascotSvg } from './MascotSvg'
import {
	STATE_PARAMS, EASING,
	BLINK_PERIOD_MS, BLINK_DURATION_MS, EYE_CENTER_Y,
} from './animations'
import { THINKING_LAYOUT, NARRATING_LAYOUT } from './icons'

const SIZE_PX: Record<MascotSize, number> = { sm: 48, md: 96, lg: 128, xl: 200 }
const THINKING_COUNT = THINKING_LAYOUT.length
const NARRATING_COUNT = NARRATING_LAYOUT.length

export function Mascot({ state = 'idle', size = 'md', onFinish, style }: MascotProps) {
	const px = SIZE_PX[size]

	// body
	const bodyScaleX = useSharedValue(1)
	const bodyScaleY = useSharedValue(1)
	const bodyTranslateY = useSharedValue(0)
	// shadow（仅 enter 状态可见；其他状态 opacity/scale 均归 0）
	const shadowScale = useSharedValue(1)
	const shadowOpacity = useSharedValue(0)
	// blink
	const blinkScaleY = useSharedValue(1)
	// eyes 4 形态 opacity
	const eyesOpenOpacity = useSharedValue(1)
	const eyesSquintOpacity = useSharedValue(0)
	const eyesSmileOpacity = useSharedValue(0)
	const eyesCrescentOpacity = useSharedValue(0)

	// All 7 thinking SVs — always called unconditionally to satisfy hooks rules
	const thinkingSV0 = useSharedValue(0)
	const thinkingSV1 = useSharedValue(0)
	const thinkingSV2 = useSharedValue(0)
	const thinkingSV3 = useSharedValue(0)
	const thinkingSV4 = useSharedValue(0)
	const thinkingSV5 = useSharedValue(0)
	const thinkingSV6 = useSharedValue(0)
	const thinkingSVs = [thinkingSV0, thinkingSV1, thinkingSV2, thinkingSV3, thinkingSV4, thinkingSV5, thinkingSV6]

	// All narrating SVs — always called
	const narratingSV0 = useSharedValue(0)
	const narratingSV1 = useSharedValue(0)
	const narratingSV2 = useSharedValue(0)
	const narratingSVs = [narratingSV0, narratingSV1, narratingSV2]

	useEffect(() => {
		const params = STATE_PARAMS[state]

		// —— 取消所有正在跑的动画 ——
		cancelAnimation(bodyScaleX)
		cancelAnimation(bodyScaleY)
		cancelAnimation(bodyTranslateY)
		cancelAnimation(shadowScale)
		cancelAnimation(shadowOpacity)
		cancelAnimation(blinkScaleY)
		thinkingSVs.forEach(cancelAnimation)
		narratingSVs.forEach(cancelAnimation)

		// —— body 动画 ——
		if (params.isOneTime && state === 'enter') {
			bodyTranslateY.value = -200
			bodyScaleX.value = 1
			bodyScaleY.value = 1
			shadowScale.value = 0.2
			shadowOpacity.value = 0

			bodyTranslateY.value = withTiming(0, { duration: 600, easing: EASING.fall })
			shadowScale.value = withTiming(1, { duration: 600, easing: EASING.fall })
			shadowOpacity.value = withSequence(
				withTiming(0.4, { duration: 600, easing: EASING.fall }),
				withDelay(120, withTiming(0, { duration: 270, easing: EASING.standard })),
			)

			bodyScaleX.value = withSequence(
				withDelay(600, withTiming(1.15, { duration: 120, easing: EASING.standard })),
				withTiming(0.92, { duration: 120, easing: EASING.standard }),
				withTiming(1, { duration: 150, easing: EASING.standard }, (finished) => {
					if (finished && onFinish) runOnJS(onFinish)()
				}),
			)
			bodyScaleY.value = withSequence(
				withDelay(600, withTiming(0.85, { duration: 120, easing: EASING.standard })),
				withTiming(1.15, { duration: 120, easing: EASING.standard }),
				withTiming(1, { duration: 150, easing: EASING.standard }),
			)
		} else if (params.isOneTime && state === 'done') {
			shadowScale.value = withTiming(0, { duration: 200 })
			shadowOpacity.value = withTiming(0, { duration: 200 })
			bodyTranslateY.value = withTiming(0, { duration: 200 })
			bodyScaleX.value = withSequence(
				withTiming(1.05, { duration: 200, easing: EASING.standard }),
				withTiming(1, { duration: 300, easing: EASING.standard }, (finished) => {
					if (finished && onFinish) runOnJS(onFinish)()
				}),
			)
			bodyScaleY.value = withSequence(
				withTiming(1.05, { duration: 200, easing: EASING.standard }),
				withTiming(1, { duration: 300, easing: EASING.standard }),
			)
		} else if (params.body.loop) {
			shadowScale.value = withTiming(0, { duration: 200 })
			shadowOpacity.value = withTiming(0, { duration: 200 })

			bodyScaleX.value = withRepeat(
				withSequence(
					withTiming(params.body.scaleX, { duration: params.body.duration, easing: EASING.loopSoft }),
					withTiming(1, { duration: params.body.duration, easing: EASING.loopSoft }),
				), -1, false,
			)
			bodyScaleY.value = withRepeat(
				withSequence(
					withTiming(params.body.scaleY, { duration: params.body.duration, easing: EASING.loopSoft }),
					withTiming(1, { duration: params.body.duration, easing: EASING.loopSoft }),
				), -1, false,
			)
			bodyTranslateY.value = withRepeat(
				withSequence(
					withTiming(params.body.translateY, { duration: params.body.duration, easing: EASING.loopSoft }),
					withTiming(0, { duration: params.body.duration, easing: EASING.loopSoft }),
				), -1, false,
			)
		} else {
			shadowScale.value = withTiming(0, { duration: 200 })
			shadowOpacity.value = withTiming(0, { duration: 200 })
			bodyScaleX.value = withTiming(params.body.scaleX, { duration: params.body.duration, easing: EASING.standard })
			bodyScaleY.value = withTiming(params.body.scaleY, { duration: params.body.duration, easing: EASING.standard })
			bodyTranslateY.value = withTiming(params.body.translateY, { duration: params.body.duration, easing: EASING.standard })
		}

		// —— blink ——
		if (params.blinkActive) {
			blinkScaleY.value = withRepeat(
				withSequence(
					withTiming(1, { duration: BLINK_PERIOD_MS - BLINK_DURATION_MS }),
					withTiming(0.1, { duration: BLINK_DURATION_MS / 2 }),
					withTiming(1, { duration: BLINK_DURATION_MS / 2 }),
				), -1, false,
			)
		} else {
			blinkScaleY.value = withTiming(1, { duration: 100 })
		}

		// —— eyes 形态切换（150ms crossfade） ——
		eyesOpenOpacity.value     = withTiming(params.eyes === 'open'     ? 1 : 0, { duration: 150 })
		eyesSquintOpacity.value   = withTiming(params.eyes === 'squint'   ? 1 : 0, { duration: 150 })
		eyesSmileOpacity.value    = withTiming(params.eyes === 'smile'    ? 1 : 0, { duration: 150 })
		eyesCrescentOpacity.value = withTiming(params.eyes === 'crescent' ? 1 : 0, { duration: 150 })

		// —— thinking 抛物线（仅操作前 THINKING_COUNT 个） ——
		if (params.thinkingActive) {
			for (let i = 0; i < THINKING_COUNT; i++) {
				const item = THINKING_LAYOUT[i]
				thinkingSVs[i].value = 0
				thinkingSVs[i].value = withDelay(
					item.delay,
					withRepeat(
						withTiming(1, { duration: item.durationMs, easing: EASING.parabolic }),
						-1, false,
					),
				)
			}
		} else {
			for (let i = 0; i < THINKING_COUNT; i++) {
				thinkingSVs[i].value = withTiming(0, { duration: 200 })
			}
		}

		// —— narrating 直上（仅操作前 NARRATING_COUNT 个） ——
		if (params.narratingActive) {
			for (let i = 0; i < NARRATING_COUNT; i++) {
				const item = NARRATING_LAYOUT[i]
				narratingSVs[i].value = 0
				narratingSVs[i].value = withDelay(
					item.delay,
					withRepeat(
						withTiming(1, { duration: item.durationMs, easing: EASING.standard }),
						-1, false,
					),
				)
			}
		} else {
			for (let i = 0; i < NARRATING_COUNT; i++) {
				narratingSVs[i].value = withTiming(0, { duration: 200 })
			}
		}
	}, [state])

	// ─── animatedProps ──────────────────────────────────────────────────────────
	// Fabric 模式下 react-native-svg 的 transform 属性需要数组形式
	// { prop: sharedValue } 句法让 Reanimated 自动 unwrap SharedValue

	const bodyAnimatedProps = useAnimatedProps(() => ({
		opacity: 1,
		transform: [
			{ translateX: 0 },
			{ translateY: bodyTranslateY.value },
			{ scaleX: bodyScaleX.value },
			{ scaleY: bodyScaleY.value },
		],
	}))

	const shadowAnimatedProps = useAnimatedProps(() => ({
		opacity: shadowOpacity.value,
		transform: [
			{ translateX: 100 },
			{ translateY: 195 },
			{ scale: shadowScale.value },
		],
	}))

	const blinkAnimatedProps = useAnimatedProps(() => ({
		transform: [
			{ translateY: EYE_CENTER_Y },
			{ scaleY: blinkScaleY.value },
			{ translateY: -EYE_CENTER_Y },
		],
	}))

	const eyesOpenAnimatedProps     = useAnimatedProps(() => ({ opacity: eyesOpenOpacity.value }))
	const eyesSquintAnimatedProps   = useAnimatedProps(() => ({ opacity: eyesSquintOpacity.value }))
	const eyesSmileAnimatedProps   = useAnimatedProps(() => ({ opacity: eyesSmileOpacity.value }))
	const eyesCrescentAnimatedProps = useAnimatedProps(() => ({ opacity: eyesCrescentOpacity.value }))

	// thinking — one useAnimatedProps per slot, always called
	// matrix: "scaleX skewY skewX scaleY translateX translateY"
	// Use string matrix so TypeScript infers single type; Fabric accepts matrix=[...]
	const thinkingAP0 = useAnimatedProps(() => {
		const item = THINKING_LAYOUT[0]
		const p = thinkingSVs[0].value
		const x = item.startX + (item.peakX - item.startX) * p
		const y = item.startY + (item.peakY - item.startY) * p
		const s = item.scaleStart + (item.scaleEnd - item.scaleStart) * p
		const r = item.rotateEnd * p
		const cosR = Math.cos(r * Math.PI / 180)
		const sinR = Math.sin(r * Math.PI / 180)
		const opacity = p < 0.2 ? p * 5 : p > 0.7 ? Math.max(0, (1 - p) / 0.3) : 1
		return { opacity, matrix: [s * cosR, s * sinR, -s * sinR, s * cosR, x, y] }
	})
	const thinkingAP1 = useAnimatedProps(() => {
		if (THINKING_COUNT <= 1) return { opacity: 0, matrix: [0, 0, 0, 0, 0, 0] }
		const item = THINKING_LAYOUT[1]
		const p = thinkingSVs[1].value
		const x = item.startX + (item.peakX - item.startX) * p
		const y = item.startY + (item.peakY - item.startY) * p
		const s = item.scaleStart + (item.scaleEnd - item.scaleStart) * p
		const r = item.rotateEnd * p
		const cosR = Math.cos(r * Math.PI / 180)
		const sinR = Math.sin(r * Math.PI / 180)
		const opacity = p < 0.2 ? p * 5 : p > 0.7 ? Math.max(0, (1 - p) / 0.3) : 1
		return { opacity, matrix: [s * cosR, s * sinR, -s * sinR, s * cosR, x, y] }
	})
	const thinkingAP2 = useAnimatedProps(() => {
		if (THINKING_COUNT <= 2) return { opacity: 0, matrix: [0, 0, 0, 0, 0, 0] }
		const item = THINKING_LAYOUT[2]
		const p = thinkingSVs[2].value
		const x = item.startX + (item.peakX - item.startX) * p
		const y = item.startY + (item.peakY - item.startY) * p
		const s = item.scaleStart + (item.scaleEnd - item.scaleStart) * p
		const r = item.rotateEnd * p
		const cosR = Math.cos(r * Math.PI / 180)
		const sinR = Math.sin(r * Math.PI / 180)
		const opacity = p < 0.2 ? p * 5 : p > 0.7 ? Math.max(0, (1 - p) / 0.3) : 1
		return { opacity, matrix: [s * cosR, s * sinR, -s * sinR, s * cosR, x, y] }
	})
	const thinkingAP3 = useAnimatedProps(() => {
		if (THINKING_COUNT <= 3) return { opacity: 0, matrix: [0, 0, 0, 0, 0, 0] }
		const item = THINKING_LAYOUT[3]
		const p = thinkingSVs[3].value
		const x = item.startX + (item.peakX - item.startX) * p
		const y = item.startY + (item.peakY - item.startY) * p
		const s = item.scaleStart + (item.scaleEnd - item.scaleStart) * p
		const r = item.rotateEnd * p
		const cosR = Math.cos(r * Math.PI / 180)
		const sinR = Math.sin(r * Math.PI / 180)
		const opacity = p < 0.2 ? p * 5 : p > 0.7 ? Math.max(0, (1 - p) / 0.3) : 1
		return { opacity, matrix: [s * cosR, s * sinR, -s * sinR, s * cosR, x, y] }
	})
	const thinkingAP4 = useAnimatedProps(() => {
		if (THINKING_COUNT <= 4) return { opacity: 0, matrix: [0, 0, 0, 0, 0, 0] }
		const item = THINKING_LAYOUT[4]
		const p = thinkingSVs[4].value
		const x = item.startX + (item.peakX - item.startX) * p
		const y = item.startY + (item.peakY - item.startY) * p
		const s = item.scaleStart + (item.scaleEnd - item.scaleStart) * p
		const r = item.rotateEnd * p
		const cosR = Math.cos(r * Math.PI / 180)
		const sinR = Math.sin(r * Math.PI / 180)
		const opacity = p < 0.2 ? p * 5 : p > 0.7 ? Math.max(0, (1 - p) / 0.3) : 1
		return { opacity, matrix: [s * cosR, s * sinR, -s * sinR, s * cosR, x, y] }
	})
	const thinkingAP5 = useAnimatedProps(() => {
		if (THINKING_COUNT <= 5) return { opacity: 0, matrix: [0, 0, 0, 0, 0, 0] }
		const item = THINKING_LAYOUT[5]
		const p = thinkingSVs[5].value
		const x = item.startX + (item.peakX - item.startX) * p
		const y = item.startY + (item.peakY - item.startY) * p
		const s = item.scaleStart + (item.scaleEnd - item.scaleStart) * p
		const r = item.rotateEnd * p
		const cosR = Math.cos(r * Math.PI / 180)
		const sinR = Math.sin(r * Math.PI / 180)
		const opacity = p < 0.2 ? p * 5 : p > 0.7 ? Math.max(0, (1 - p) / 0.3) : 1
		return { opacity, matrix: [s * cosR, s * sinR, -s * sinR, s * cosR, x, y] }
	})
	const thinkingAP6 = useAnimatedProps(() => {
		if (THINKING_COUNT <= 6) return { opacity: 0, matrix: [0, 0, 0, 0, 0, 0] }
		const item = THINKING_LAYOUT[6]
		const p = thinkingSVs[6].value
		const x = item.startX + (item.peakX - item.startX) * p
		const y = item.startY + (item.peakY - item.startY) * p
		const s = item.scaleStart + (item.scaleEnd - item.scaleStart) * p
		const r = item.rotateEnd * p
		const cosR = Math.cos(r * Math.PI / 180)
		const sinR = Math.sin(r * Math.PI / 180)
		const opacity = p < 0.2 ? p * 5 : p > 0.7 ? Math.max(0, (1 - p) / 0.3) : 1
		return { opacity, matrix: [s * cosR, s * sinR, -s * sinR, s * cosR, x, y] }
	})

	const narratingAP0 = useAnimatedProps(() => {
		if (NARRATING_COUNT === 0) return { opacity: 0, matrix: [0, 0, 0, 0, 0, 0] }
		const item = NARRATING_LAYOUT[0]
		const p = narratingSVs[0].value
		const y = item.startY + (item.endY - item.startY) * p
		const s = item.scaleStart + (item.scaleEnd - item.scaleStart) * p
		const opacity = p < 0.15 ? p / 0.15 : p > 0.6 ? Math.max(0, (1 - p) / 0.4) : 1
		return { opacity, matrix: [s, 0, 0, s, item.x, y] }
	})
	const narratingAP1 = useAnimatedProps(() => {
		if (NARRATING_COUNT <= 1) return { opacity: 0, matrix: [0, 0, 0, 0, 0, 0] }
		const item = NARRATING_LAYOUT[1]
		const p = narratingSVs[1].value
		const y = item.startY + (item.endY - item.startY) * p
		const s = item.scaleStart + (item.scaleEnd - item.scaleStart) * p
		const opacity = p < 0.15 ? p / 0.15 : p > 0.6 ? Math.max(0, (1 - p) / 0.4) : 1
		return { opacity, matrix: [s, 0, 0, s, item.x, y] }
	})
	const narratingAP2 = useAnimatedProps(() => {
		if (NARRATING_COUNT <= 2) return { opacity: 0, matrix: [0, 0, 0, 0, 0, 0] }
		const item = NARRATING_LAYOUT[2]
		const p = narratingSVs[2].value
		const y = item.startY + (item.endY - item.startY) * p
		const s = item.scaleStart + (item.scaleEnd - item.scaleStart) * p
		const opacity = p < 0.15 ? p / 0.15 : p > 0.6 ? Math.max(0, (1 - p) / 0.4) : 1
		return { opacity, matrix: [s, 0, 0, s, item.x, y] }
	})

	// Slice to actual used length
	const thinkingAnimatedProps = [thinkingAP0, thinkingAP1, thinkingAP2, thinkingAP3, thinkingAP4, thinkingAP5, thinkingAP6].slice(0, THINKING_COUNT)
	const narratingAnimatedProps = [narratingAP0, narratingAP1, narratingAP2].slice(0, NARRATING_COUNT)

	return (
		<View style={[{ width: px, height: px }, style]}>
			<MascotSvg
				bodyAnimatedProps={bodyAnimatedProps}
				shadowAnimatedProps={shadowAnimatedProps}
				blinkAnimatedProps={blinkAnimatedProps}
				eyesOpenAnimatedProps={eyesOpenAnimatedProps}
				eyesSquintAnimatedProps={eyesSquintAnimatedProps}
				eyesSmileAnimatedProps={eyesSmileAnimatedProps}
				eyesCrescentAnimatedProps={eyesCrescentAnimatedProps}
				thinkingAnimatedProps={thinkingAnimatedProps}
				narratingAnimatedProps={narratingAnimatedProps}
			/>
		</View>
	)
}
