import type { Theme } from './theme'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type StylesFactory<T extends object> = (theme: Theme) => T
