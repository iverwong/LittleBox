// 占位实现。外包交付 Lottie JSON 后，本文件内部替换为 lottie-react-native 实现。
// 调用方 API（Props / MascotState / MascotSize）必须保持兼容，
// 不得破坏 state / size / onFinish 接口。契约定义在 Mascot.types.ts，替换时不得修改。

import { Feather } from '@expo/vector-icons'
import { useMemo, useEffect, useRef } from 'react'
import { View } from 'react-native'
import { useTheme } from '@/theme'
import { createStyles, getMascotSize, getMascotIconSize } from './Mascot.styles'
import type { MascotProps } from './Mascot.types'

// One-time states trigger onFinish after 500ms; loop states do not.
const ONE_TIME_STATES = new Set(['enter', 'done'])

export function Mascot({ state = 'idle', size = 'md', onFinish, style }: MascotProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])
	const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

	const containerSize = getMascotSize(size)
	const iconSize = getMascotIconSize(size)
	const halfSize = containerSize / 2

	// Restart 500ms timer whenever a one-time state changes
	useEffect(() => {
		if (ONE_TIME_STATES.has(state)) {
			timerRef.current = setTimeout(() => {
				onFinish?.()
			}, 500)
		}
		return () => {
			if (timerRef.current !== null) {
				clearTimeout(timerRef.current)
				timerRef.current = null
			}
		}
	}, [state, onFinish])

	return (
		<View
			style={[
				styles.container,
				{
					width: containerSize,
					height: containerSize,
				},
				style as object,
			]}
		>
			<View
				style={[
					styles.outerRing,
					{
						width: containerSize,
						height: containerSize,
						borderRadius: halfSize,
					},
				]}
			>
				<View
					style={[
						styles.innerCircle,
						{
							width: containerSize - 4,
							height: containerSize - 4,
							borderRadius: halfSize - 2,
						},
					]}
				>
					<Feather name="smile" size={iconSize} color={theme.palette.primary[600]} />
				</View>
			</View>
		</View>
	)
}
