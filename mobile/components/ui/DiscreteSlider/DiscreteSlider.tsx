import { useMemo } from 'react'
import { View, Text, Pressable } from 'react-native'
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

export function DiscreteSlider({ nodes, value, onValueChange, disabled, leftLabel, rightLabel, centerLabel }: DiscreteSliderProps) {
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

	const pct = nodeCount > 1 ? (activeIndex / (nodeCount - 1)) * 100 : 0

	return (
		<View style={styles.container}>
			{/* 中央 label */}
			<View style={styles.centerLabelRow}>
				{renderCenter(centerLabel, value, styles)}
			</View>

			{/* 轨道行 */}
			<View style={styles.trackRow}>
				{/* 轨道背景 */}
				<View style={styles.trackBg} />
				{/* 激活段 */}
				<View style={[styles.activeTrack, { width: `${pct}%` }]} />
				{/* thumb */}
				<View
					style={[
						styles.thumbOuter,
						{ left: `${pct}%`, marginLeft: -15 },
						disabled && { opacity: 0.6 },
					]}
				>
					<View style={styles.thumb} />
				</View>
				{/* 节点 */}
				{nodes.map((nodeVal, i) => {
					const nodePct = nodeCount > 1 ? (i / (nodeCount - 1)) * 100 : 0
					return (
						<Pressable
							key={i}
							style={[styles.nodePressable, { left: `${nodePct}%`, marginLeft: -13 }]}
							onPress={() => onValueChange(nodeVal)}
							disabled={disabled}
						>
							<View style={styles.node} />
						</Pressable>
					)
				})}
			</View>

			{/* 左右 label */}
			<View style={styles.bottomRow}>
				{leftLabel !== undefined ? (
					leftLabel
				) : (
					<Text style={styles.leftLabelText}>{String(nodes[0])}</Text>
				)}
				{rightLabel !== undefined ? (
					rightLabel
				) : (
					<Text style={styles.rightLabelText}>{String(nodes[nodes.length - 1])}</Text>
				)}
			</View>
		</View>
	)
}
