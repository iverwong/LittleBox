import type { ReactNode } from 'react'

export interface ListItemProps {
	leading?: ReactNode
	title: string
	subtitle?: string
	trailing?: ReactNode
	onPress?: () => void
	divider?: boolean
}
