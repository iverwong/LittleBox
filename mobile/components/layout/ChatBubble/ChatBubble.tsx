import { View, Text } from 'react-native'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './ChatBubble.styles'
import type { ChatBubbleProps, BubbleRole } from './ChatBubble.types'
export type { BubbleRole, ChatBubbleProps }

export function ChatBubble({ role, children }: ChatBubbleProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])

	const isUser = role === 'user'

	return (
		<View style={[styles.wrap, isUser ? styles.wrapUser : styles.wrapAi]}>
			<View style={[styles.bubble, isUser ? styles.bubbleUser : styles.bubbleAi]}>
				<Text style={[styles.text, isUser ? styles.textUser : styles.textAi]}>{children}</Text>
			</View>
		</View>
	)
}
