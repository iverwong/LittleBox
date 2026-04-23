import { Feather } from '@expo/vector-icons'
import { Pressable, Text } from 'react-native'
import { useMemo } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './Button.styles'
import type { ButtonProps } from './Button.types'
import type { FeatherName } from '../types'
import { Loading } from '@/components/ui/Loading'

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
				style,
			]}
		>
			{loading ? (
				<Loading size="sm" />
			) : (
				<>
					{leftIcon && (
						<Feather
							name={leftIcon as FeatherName}
							size={18}
							color={textColor}
							style={styles.icon}
						/>
					)}
					{children != null && (
						<Text style={{ color: textColor, fontSize: theme.typography.fontSize.base }}>
							{children}
						</Text>
					)}
					{rightIcon && (
						<Feather
							name={rightIcon as FeatherName}
							size={18}
							color={textColor}
							style={styles.icon}
						/>
					)}
				</>
			)}
		</Pressable>
	)
}
