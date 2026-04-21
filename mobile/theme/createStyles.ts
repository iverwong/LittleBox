import type { StyleSheet } from 'react-native'
import type { Theme } from './theme'

export type StylesFactory<T extends StyleSheet.NamedStyles<T>> = (theme: Theme) => T
