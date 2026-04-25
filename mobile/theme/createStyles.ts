import type { Theme } from './theme'

export type StylesFactory<T extends object> = (theme: Theme) => T
