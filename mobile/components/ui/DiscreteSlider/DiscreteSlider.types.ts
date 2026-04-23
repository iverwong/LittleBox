import type { ReactNode } from 'react'
import type { StyleProp, ViewStyle } from 'react-native'

export interface DiscreteSliderProps {
	/** 节点值列表；严格递增；至少 2 档 */
	nodes: readonly number[]
	/** 当前值；必须在 nodes 内，否则 fallback 到最接近的节点并在 __DEV__ 下 warn */
	value: number
	/** 吸附值变化时触发（不每帧） */
	onValueChange: (value: number) => void
	/** 整体灰显 + 不可交互 */
	disabled?: boolean
	/** 极小值 label；默认渲染 String(nodes[0]) */
	leftLabel?: ReactNode
	/** 极大值 label；默认渲染 String(nodes[last]) */
	rightLabel?: ReactNode
	/** 中央 label；支持 string / ReactNode / (value) => ReactNode 三态；默认渲染 String(value) */
	centerLabel?: string | ReactNode | ((value: number) => ReactNode)
	/** 三个 label 显示开关；默认 true；关闭时对应区域不占高度 */
	showLeftLabel?: boolean
	showRightLabel?: boolean
	showCenterLabel?: boolean
	style?: StyleProp<ViewStyle>
}
