import { Feather } from '@expo/vector-icons'
import { Pressable, Text, ActivityIndicator } from 'react-native'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './Button.styles'
import type { ButtonProps } from './Button.types'

const VARIANT_TEXT_COLOR: Record<string, string> = {
	primary: '#FFFFFF',
	secondary: '#FFFFFF',
	ghost: '', // resolved dynamically below
	danger: '#FFFFFF',
}

export function Button({
	variant = 'primary',
	size = 'md',
	loading = false,
	disabled = false,
	leftIcon,
	rightIcon,
	onPress,
	children,
	style,
}: ButtonProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])
	const isDisabled = disabled || loading

	const ghostColor = theme.palette.secondary[600]
	const textColor = variant === 'ghost' ? ghostColor : VARIANT_TEXT_COLOR[variant]

	return (
		<Pressable
			onPress={isDisabled ? undefined : onPress}
			disabled={isDisabled}
			style={({ pressed }) => [
				styles.base,
				styles[`variant_${variant}`],
				styles[`size_${size}`],
				pressed && !isDisabled ? styles.pressed : null,
				isDisabled ? styles.disabled : null,
				style as object,
			]}
		>
			{loading ? (
				<ActivityIndicator size="small" color={variant === 'ghost' ? ghostColor : '#FFFFFF'} />
			) : (
				<>
					{leftIcon && (
						<Feather
							name={leftIcon as keyof typeof Feather.glyphMap}
							size={18}
							color={textColor}
							style={styles.icon as object}
						/>
					)}
					{children != null && (
						<Text style={{ color: textColor, fontSize: theme.typography.fontSize.base }}>
							{children}
						</Text>
					)}
					{rightIcon && (
						<Feather
							name={rightIcon as keyof typeof Feather.glyphMap}
							size={18}
							color={textColor}
							style={styles.icon as object}
						/>
					)}
				</>
			)}
		</Pressable>
	)
}
