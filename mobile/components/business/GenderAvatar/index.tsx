/**
 * GenderAvatar — circular icon avatar for child gender display.
 *
 * Props:
 *   gender: 'male' | 'female' | 'unknown'
 *   size: diameter in px (default 48)
 *   selected: true → outer border ring (used in new-child form three-way selector)
 *
 * Icon mapping (Ionicons):
 *   male     → "man"
 *   female   → "woman"
 *   unknown  → "help"
 *
 * Colors (hardcoded per plan §图标实现约定; M14 may move to design token):
 *   male     bg #DBEAFE / icon #2563EB
 *   female   bg #FCE7F3 / icon #DB2777
 *   unknown  bg secondary[100] / icon secondary[500]
 */
import { View } from 'react-native'
import { Ionicons } from '@expo/vector-icons'
import { useTheme } from '@/theme'

interface GenderAvatarProps {
  gender: 'male' | 'female' | 'unknown'
  size?: number
  selected?: boolean
}

const MALE_BG = '#DBEAFE'
const MALE_COLOR = '#2563EB'
const FEMALE_BG = '#FCE7F3'
const FEMALE_COLOR = '#DB2777'

export function GenderAvatar({ gender, size = 48, selected = false }: GenderAvatarProps) {
  const theme = useTheme()

  const iconSize = Math.round(size * 0.58)

  let bg: string
  let iconColor: string
  let iconName: keyof typeof Ionicons.glyphMap

  switch (gender) {
    case 'male':
      bg = MALE_BG
      iconColor = MALE_COLOR
      iconName = 'man'
      break
    case 'female':
      bg = FEMALE_BG
      iconColor = FEMALE_COLOR
      iconName = 'woman'
      break
    case 'unknown':
    default:
      bg = theme.palette.secondary[100]
      iconColor = theme.palette.secondary[500]
      iconName = 'help'
      break
  }

  return (
    <View
      style={{
        width: size,
        height: size,
        borderRadius: size / 2,
        backgroundColor: bg,
        alignItems: 'center',
        justifyContent: 'center',
        ...(selected
          ? { borderWidth: 2, borderColor: theme.palette.primary[500] }
          : null),
      }}
    >
      <Ionicons name={iconName} size={iconSize} color={iconColor} />
    </View>
  )
}
