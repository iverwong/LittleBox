/**
 * GenderAvatar — 圆形性别头像，统一性别相关 UI 入口（列表 / 添加 / 编辑）。
 *
 * 内部使用项目本地 SVG 图标（GenderBoy / GenderGirl / GenderUnknown），
 * 替代旧的 Ionicons 渲染。
 *
 * 配色（保留性别区分度，便于列表中并列展示）：
 *   male    bg #DBEAFE / icon #2563EB
 *   female  bg #FCE7F3 / icon #DB2777
 *   unknown bg neutral[100] / icon neutral[500]
 *
 * Props:
 *   gender:   'male' | 'female' | 'unknown'
 *   size:     直径（px），默认 48
 *   selected: true → 外层加 primary ring（用于性别选择态）
 */
import { View } from 'react-native'
import { useTheme } from '@/theme'
import { GenderBoy } from '@/components/icons/GenderBoy'
import { GenderGirl } from '@/components/icons/GenderGirl'
import { GenderUnknown } from '@/components/icons/GenderUnknown'

interface GenderAvatarProps {
  gender: 'male' | 'female' | 'unknown'
  size?: number
  selected?: boolean
}

// 调整后：低饱和、暖化
const MALE_BG = '#E3EAEF'      // 雾蓝（饱和度极低）
const MALE_COLOR = '#647D94'   // 蓝灰
const FEMALE_BG = '#EFD9CD'    // 裸粉 / 陶土粉（暖色调）
const FEMALE_COLOR = '#A36B5A' // 暖玫（接近棕红）
// unknown 维持 neutral[100] / neutral[500] 不变

export function GenderAvatar({ gender, size = 48, selected = false }: GenderAvatarProps) {
  const theme = useTheme()
  const iconSize = Math.round(size * 0.58)

  let bg: string
  let iconColor: string
  let Icon: typeof GenderBoy

  switch (gender) {
    case 'male':
      bg = MALE_BG
      iconColor = MALE_COLOR
      Icon = GenderBoy
      break
    case 'female':
      bg = FEMALE_BG
      iconColor = FEMALE_COLOR
      Icon = GenderGirl
      break
    case 'unknown':
    default:
      bg = theme.palette.neutral[100]
      iconColor = theme.palette.neutral[500]
      Icon = GenderUnknown
      break
  }

  return (
    <View
      style=
      {{
        width: size,
        height: size,
        borderRadius: size / 2,
        backgroundColor: bg,
        alignItems: 'center',
        justifyContent: 'center',
        borderWidth: 2,
        borderColor: selected ? theme.palette.primary[500] : 'transparent',
      }}

    >
      <Icon color={iconColor} size={iconSize} />
    </View >
  )
}