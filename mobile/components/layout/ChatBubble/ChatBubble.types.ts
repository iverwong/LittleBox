import type { ReactNode } from 'react'

export type BubbleRole = 'user' | 'ai'

export interface ChatBubbleProps {
	role: BubbleRole
	children: ReactNode
}
