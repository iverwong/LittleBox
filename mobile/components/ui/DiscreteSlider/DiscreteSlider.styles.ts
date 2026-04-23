import { StyleSheet } from 'react-native'
import type { Theme } from '@/theme'

type DiscreteSliderStyles = Record<string, never>

export const createStyles = (_theme: Theme): DiscreteSliderStyles => {
	return StyleSheet.create({})
}
