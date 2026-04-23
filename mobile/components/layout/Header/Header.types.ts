import type { ReactNode } from 'react'

export interface HeaderProps {
	title?: string
	subtitle?: string
	leading?: ReactNode
	trailing?: ReactNode
	safeArea?: boolean
}
