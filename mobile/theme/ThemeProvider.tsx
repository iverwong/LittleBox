import { createContext, useMemo, type ReactNode } from 'react'
import { theme, type Theme } from './theme'

export const ThemeContext = createContext<Theme | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
	const value = useMemo(() => theme, [])
	return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}
