import { useCallback, useEffect, useMemo } from 'react'
import type { GestureResponderEvent, LayoutChangeEvent } from 'react-native'
import { Pressable, StyleSheet, View, Text } from 'react-native'
import { Gesture, GestureDetector } from 'react-native-gesture-handler'
import Animated, {
	runOnJS,
	useAnimatedReaction,
	useAnimatedStyle,
	useSharedValue,
	withTiming,
} from 'react-native-reanimated'
import { useTheme } from '@/theme'
import type { DiscreteSliderProps } from './DiscreteSlider.types'
import { createStyles } from './DiscreteSlider.styles'

/** 在 nodes 中查找距 value 最近的节点索引。平手时返回更小的索引。 */
function findNearestIndex(nodes: readonly number[], value: number): number {
	let best = 0
	let bestDiff = Math.abs(nodes[0] - value)
	for (let i = 1; i < nodes.length; i++) {
		const diff = Math.abs(nodes[i] - value)
		if (diff < bestDiff) {
			best = i
			bestDiff = diff
		}
	}
	return best
}

function renderCenter(
	centerLabel: DiscreteSliderProps['centerLabel'],
	value: number,
	styles: ReturnType<typeof createStyles>,
) {
	if (typeof centerLabel === 'function') return centerLabel(value)
	if (typeof centerLabel === 'string') return <Text style={styles.centerValue}>{centerLabel}</Text>
	if (centerLabel != null) return centerLabel
	return <Text style={styles.centerValue}>{String(value)}</Text>
}

const THUMB_RADIUS = 15 // thumbOuter 外层 30×30 容器半径，不是内层视觉圆 24/2

export function DiscreteSlider({ nodes, value, onValueChange, disabled, leftLabel, rightLabel, centerLabel, showLeftLabel, showRightLabel, showCenterLabel }: DiscreteSliderProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])

	const nodeCount = nodes.length
	const activeIndex = useMemo(() => {
		const idx = nodes.indexOf(value)
		if (idx < 0 && __DEV__) {
			console.warn(`[DiscreteSlider] value=${value} 不在 nodes 内，fallback 到最近节点`)
		}
		return idx >= 0 ? idx : findNearestIndex(nodes, value)
	}, [nodes, value])

	// ── shared values ────────────────────────────────────────────────────────────
	// thumbX：仅 worklet 写（pan 手势 + useAnimatedReaction 派生），被 thumbStyle / activeTrackStyle 读
	const thumbX = useSharedValue(0)
	// startX：仅 worklet 写（pan onBegin）
	const startX = useSharedValue(0)
	// trackWidthSV：仅 JS 写（onLayout），不被任何 animatedStyle 读
	const trackWidthSV = useSharedValue(0)
	// activeIndexSV：仅 JS 写（useEffect 镜像 prop），不被任何 animatedStyle 读
	const activeIndexSV = useSharedValue(activeIndex)
	// lastSnappedIndex：仅 worklet 写（pan 手势），不被任何 animatedStyle 读
	const lastSnappedIndex = useSharedValue(activeIndex)

	// ── JS 侧 notify（始终读最新 nodes） ────────────────────────────────────────
	const notifyChange = useCallback(
		(index: number) => onValueChange(nodes[index]),
		[nodes, onValueChange],
	)

	// ── activeIndexSV：镜像 prop 到 SV（替代直接写 thumbX 的旧 useEffect） ──────
	useEffect(() => {
		activeIndexSV.value = activeIndex
	}, [activeIndex])

	// ── R4：useAnimatedReaction 派生 thumbX（worklet 内写 thumbX） ───────────────
	useAnimatedReaction(
		() => ({ width: trackWidthSV.value, idx: activeIndexSV.value }),
		(curr, prev) => {
			if (curr.width <= 0 || nodeCount <= 1) return
			const target = (curr.idx / (nodeCount - 1)) * curr.width
			if (prev == null) {
				// 首次：直接写到目标位置，避免从 0 滑到目标
				thumbX.value = target
			} else if (curr.width !== prev.width || curr.idx !== prev.idx) {
				thumbX.value = withTiming(target, { duration: 200 })
			}
			lastSnappedIndex.value = curr.idx
		},
		[nodeCount],
	)

	// ── 手势 ────────────────────────────────────────────────────────────────────
	// Pan: 只对明确横向位移 activate，纵向位移让 ScrollView
	const pan = useMemo(
		() =>
			Gesture.Pan()
				.enabled(!disabled)
				.activeOffsetX([-8, 8])
				.failOffsetY([-10, 10])
				.onBegin(() => {
					'worklet'
					const width = trackWidthSV.value
					if (width <= 0 || nodeCount < 2) return
					const gap = width / (nodeCount - 1)
					startX.value = lastSnappedIndex.value * gap
				})
				.onUpdate((e) => {
					'worklet'
					const width = trackWidthSV.value
					if (width <= 0 || nodeCount < 2) return
					const x = Math.max(0, Math.min(width, startX.value + e.translationX))
					thumbX.value = x
					const gap = width / (nodeCount - 1)
					const nextIndex = Math.round(x / gap)
					if (nextIndex !== lastSnappedIndex.value) {
						lastSnappedIndex.value = nextIndex
						runOnJS(notifyChange)(nextIndex)
					}
				})
				.onEnd(() => {
					'worklet'
					const width = trackWidthSV.value
					if (width <= 0 || nodeCount < 2) return
					const gap = width / (nodeCount - 1)
					const snappedX = lastSnappedIndex.value * gap
					thumbX.value = withTiming(snappedX, { duration: 120 })
				}),
		[disabled, nodeCount, notifyChange],
	)

	// ── tap-to-jump：RN 原生 Pressable，不走 RNGH Tap ────────────────────────────
	const handleTrackPress = useCallback(
		(e: GestureResponderEvent) => {
			if (disabled) return
			const width = trackWidthSV.value
			if (width <= 0 || nodeCount < 2) return
			const x = Math.max(0, Math.min(width, e.nativeEvent.locationX))
			const gap = width / (nodeCount - 1)
			const nearest = Math.round(x / gap)
			// 通过 prop 变化 → activeIndexSV → useAnimatedReaction 自然派生 thumbX
			if (nearest !== lastSnappedIndex.value) {
				notifyChange(nearest)
			}
		},
		[disabled, nodeCount, notifyChange],
	)

	// ── track 宽度采集（只写 trackWidthSV） ────────────────────────────────────
	const onTrackLayout = useCallback((e: LayoutChangeEvent) => {
		trackWidthSV.value = e.nativeEvent.layout.width
	}, [])

	// ── 动画样式 ────────────────────────────────────────────────────────────────
	const thumbStyle = useAnimatedStyle(() => ({
		transform: [{ translateX: thumbX.value - THUMB_RADIUS }],
	}))
	const activeTrackStyle = useAnimatedStyle(() => ({
		width: thumbX.value,
	}))

	return (
		<View style={styles.container}>
			{/* 中央 label */}
			{showCenterLabel !== false && (
				<View style={styles.centerLabelRow}>
					{renderCenter(centerLabel, value, styles)}
				</View>
			)}

			{/* 轨道行 */}
			<GestureDetector gesture={pan}>
				<View
					style={[
						styles.trackRow,
						disabled && styles.trackRowDisabled,
					]}
					onLayout={onTrackLayout}
				>
					{/* 轨道背景 */}
					<View
						style={[
							styles.trackBg,
							disabled && styles.trackBgDisabled,
						]}
					/>
					{/* 激活段 */}
					<Animated.View
						style={[
							styles.activeTrack,
							activeTrackStyle,
							disabled && styles.activeTrackDisabled,
						]}
					/>
					{/* tap-to-jump: absoluteFill Pressable 在 thumb 之下、nodeDots 之上 */}
					<Pressable
						style={StyleSheet.absoluteFill}
						onPress={handleTrackPress}
					/>
					{/* thumb */}
					<Animated.View
						style={[
							styles.thumbOuter,
							disabled && styles.thumbOuterDisabled,
							thumbStyle,
						]}
					>
						<View
							style={[
								styles.thumb,
								disabled && styles.thumbDisabled,
							]}
						/>
					</Animated.View>
					{/* 节点 */}
					{nodes.map((n, i) => {
						const pct = (i / (nodeCount - 1)) * 100
						return (
							<View
								key={i}
								style={[
									styles.nodeDot,
									disabled && styles.nodeDotDisabled,
									{ left: `${pct}%` },
								]}
								pointerEvents="none"
							/>
						)
					})}
				</View>
			</GestureDetector>

			{/* 左右 label */}
			{(showLeftLabel !== false || showRightLabel !== false) && (
				<View style={styles.bottomRow}>
					{showLeftLabel !== false && (
						leftLabel ?? <Text style={styles.leftLabelText}>{String(nodes[0])}</Text>
					)}
					{showRightLabel !== false && (
						rightLabel ?? <Text style={styles.rightLabelText}>{String(nodes[nodes.length - 1])}</Text>
					)}
				</View>
			)}
		</View>
	)
}
