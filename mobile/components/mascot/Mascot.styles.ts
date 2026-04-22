import { StyleSheet } from 'react-native'
import type { Theme } from '@/theme'
import type { MascotSize } from './Mascot.types'

const SIZE_MAP: Record<MascotSize, number> = {
	sm: 48,
	md: 96,
	lg: 128,
	xl: 200,
}

export const createStyles = (_theme: Theme) => {
	return StyleSheet.create({
		container: {
			alignItems: 'center',
			justifyContent: 'center',
		},
		outerRing: {
			borderWidth: 2,
			borderColor: _theme.palette.primary[300],
			alignItems: 'center',
			justifyContent: 'center',
		},
		innerCircle: {
			backgroundColor: _theme.palette.primary[100],
			alignItems: 'center',
			justifyContent: 'center',
		},
	})
}

// Utility: get container dimensions for a given size
export const getMascotSize = (size: MascotSize): number => SIZE_MAP[size]
export const getMascotIconSize = (size: MascotSize): number => {
	const s = SIZE_MAP[size]
	return Math.round(s * 0.5)
}
