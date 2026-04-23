import type { Feather } from '@expo/vector-icons'

// Shared icon name type across all UI components
// Derives from Feather glyph map to ensure only valid icon names are used
export type FeatherName = keyof typeof Feather.glyphMap
