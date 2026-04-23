import { useCallback, useEffect, useMemo } from 'react'
import type { LayoutChangeEvent } from 'react-native'
import { View, Text, Pressable } from 'react-native'
import { Gesture, GestureDetector } from 'react-native-gesture-handler'
import Animated, {
	runOnJS,
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
	const thumbX = useSharedValue(0)
	const startX = useSharedValue(0)
	const trackWidthSV = useSharedValue(0)
	const lastSnappedIndex = useSharedValue(activeIndex)

	// ── JS 侧 notify（始终读最新 nodes） ────────────────────────────────────────
	const notifyChange = useCallback(
		(index: number) => onValueChange(nodes[index]),
		[nodes, onValueChange],
	)

	// ── 手势 ────────────────────────────────────────────────────────────────────
	const pan = useMemo(
		() =>
			Gesture.Pan()
				.enabled(!disabled)
				.minDistance(0)  // touch down 立即响应
				.onBegin((e) => {
					'worklet'
					const width = trackWidthSV.value
					if (width <= 0 || nodeCount < 2) return
					const gap = width / (nodeCount - 1)
					const x = Math.max(0, Math.min(width, e.x))  // e.x 是 GestureDetector 容器内 local X
					const nearest = Math.round(x / gap)
					const targetX = nearest * gap
					thumbX.value = withTiming(targetX, { duration: 120 })
					startX.value = targetX  // 关键:后续 onUpdate 从吸附点累加 translationX
					if (nearest !== lastSnappedIndex.value) {
						lastSnappedIndex.value = nearest
						runOnJS(notifyChange)(nearest)
					}
				})
				.onUpdate((e) => {
					'worklet'
					// 保持现状:从 startX + translationX 更新
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

	// ── track 宽度采集 + thumb 初始位置 ─────────────────────────────────────────
	const onTrackLayout = useCallback(
		(e: LayoutChangeEvent) => {
			const w = e.nativeEvent.layout.width
			trackWidthSV.value = w
			if (nodeCount > 1) {
				thumbX.value = (activeIndex / (nodeCount - 1)) * w
				lastSnappedIndex.value = activeIndex
			}
		},
		[activeIndex, nodeCount],
	)

	// ── 外部 value 变化同步 thumb ───────────────────────────────────────────────
	useEffect(() => {
		const width = trackWidthSV.value
		if (width > 0 && nodeCount > 1) {
			thumbX.value = withTiming(
				(activeIndex / (nodeCount - 1)) * width,
				{ duration: 120 },
			)
			lastSnappedIndex.value = activeIndex
		}
	}, [activeIndex, nodeCount])

	// ── 动画样式 ────────────────────────────────────────────────────────────────
	const thumbStyle = useAnimatedStyle(() => ({
		transform: [{ translateX: thumbX.value - THUMB_RADIUS }],
	}))
	const activeTrackStyle = useAnimatedStyle(() => ({
		width: thumbX.value,
	}))

	// ── 点击节点：JS 线程同步 shared value，立即对齐 ─────────────────────────
	const onNodePress = useCallback(
		(i: number) => {
			const width = trackWidthSV.value
			if (width > 0 && nodeCount > 1) {
				const targetX = (i / (nodeCount - 1)) * width
				thumbX.value = withTiming(targetX, { duration: 120 })
				lastSnappedIndex.value = i
			}
			onValueChange(nodes[i])
		},
		[nodeCount, onValueChange, nodes, trackWidthSV, thumbX, lastSnappedIndex],
	)

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
				<View style={styles.trackRow} onLayout={onTrackLayout}>
					{/* 轨道背景 */}
					<View style={styles.trackBg} />
					{/* 激活段 */}
					<Animated.View style={[styles.activeTrack, activeTrackStyle]} />
					{/* thumb */}
					<Animated.View
						style={[
							styles.thumbOuter,
							thumbStyle,
							disabled && { opacity: 0.6 },
						]}
					>
						<View style={styles.thumb} />
					</Animated.View>
					{/* 节点 */}
					{nodes.map((n, i) => {
						const pct = (i / (nodeCount - 1)) * 100
						return (
							<View
								key={i}
								style={[styles.nodeDot, { left: `${pct}%` }]}
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
						leftLabel !== undefined ? leftLabel : <Text style={styles.leftLabelText}>{String(nodes[0])}</Text>
					)}
					{showRightLabel !== false && (
						rightLabel !== undefined ? rightLabel : <Text style={styles.rightLabelText}>{String(nodes[nodes.length - 1])}</Text>
					)}
				</View>
			)}
		</View>
	)
}