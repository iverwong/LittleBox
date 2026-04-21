import type { ImageSourcePropType } from 'react-native'
import type { ReactNode } from 'react'
import type { FeatherName } from '../types'

export interface EmptyStateProps {
	illustration?: ImageSourcePropType
	icon?: FeatherName
	title: string
	description?: string
	action?: ReactNode
}
