import { useContext } from 'react'
import { ThemeContext } from './ThemeProvider'

export function useTheme() {
	const ctx = useContext(ThemeContext)
	if (!ctx) throw new Error('useTheme 必须在 <ThemeProvider> 内调用')
	return ctx
}
