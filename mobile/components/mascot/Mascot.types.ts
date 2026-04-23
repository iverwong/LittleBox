import type { StyleProp, ViewStyle } from 'react-native'

// 对齐外包 Brief v1 的动效状态集
export type MascotState =
	| 'enter'      // 欢迎入场（一次性，1.2-1.8s）
	| 'idle'       // 静止呼吸（循环）
	| 'listen'     // 看向输入框（循环）
	| 'thinking'   // AI 思考（循环）
	| 'narrating'  // 叙述中（循环，可无限延长）
	| 'done'       // 完成收束（一次性，0.6-1s）

export type MascotSize = 'sm' | 'md' | 'lg' | 'xl'   // 48 / 96 / 128 / 200 px

export interface MascotProps {
	/** 默认 'idle' */
	state?: MascotState
	/** 默认 'md' */
	size?: MascotSize
	/** 一次性动效（enter / done）结束回调；循环态不触发 */
	onFinish?: () => void
	style?: StyleProp<ViewStyle>
}
