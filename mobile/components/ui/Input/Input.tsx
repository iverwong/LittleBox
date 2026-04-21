import { Feather } from '@expo/vector-icons'
import { TextInput, View, Text } from 'react-native'
import { useMemo, useState } from 'react'
import { useTheme } from '@/theme'
import { createStyles } from './Input.styles'
import type { InputProps } from './Input.types'
import type { FeatherName } from '../types'

export function Input({
	value,
	onChangeText,
	placeholder,
	label,
	error,
	leftIcon,
	rightIcon,
	size = 'md',
	secureTextEntry = false,
	keyboardType = 'default',
	disabled = false,
	style,
}: InputProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])
	const [focused, setFocused] = useState(false)

	const containerStyle = useMemo(() => {
		if (error) return { ...styles.container, borderColor: theme.ui.error }
		if (focused) return {
			...styles.container,
			borderColor: theme.palette.primary[400],
			shadowColor: theme.palette.primary[500],
			shadowOpacity: 0.15,
			shadowRadius: 3,
			shadowOffset: { width: 0, height: 0 },
			elevation: 2,
		}
		if (disabled) return { ...styles.container, backgroundColor: theme.palette.neutral[100], borderColor: theme.palette.neutral[200] }
		return styles.container
	}, [styles, error, focused, disabled, theme])

	const iconColor = disabled ? theme.palette.neutral[400] : theme.palette.neutral[500]
	const textColor = disabled ? theme.palette.neutral[500] : theme.palette.neutral[800]

	return (
		<View style={[styles.wrap as object, style as object]}>
			{label && (
				<Text style={{ fontSize: theme.typography.fontSize.sm, color: theme.palette.neutral[700], marginBottom: 4 }}>
					{label}
				</Text>
			)}
			<View style={containerStyle as object}>
				{leftIcon && (
					<Feather
						name={leftIcon as FeatherName}
						size={18}
						color={iconColor}
						style={styles.iconLeft as object}
					/>
				)}
				<TextInput
					value={value}
					onChangeText={onChangeText}
					placeholder={placeholder}
					placeholderTextColor={theme.palette.neutral[400]}
					secureTextEntry={secureTextEntry}
					keyboardType={keyboardType}
					editable={!disabled}
					onFocus={() => setFocused(true)}
					onBlur={() => setFocused(false)}
					style={[styles.input, size === 'lg' ? styles.size_lg : styles.size_md, { color: textColor }] as object}
				/>
				{rightIcon && (
					<Feather
						name={rightIcon as FeatherName}
						size={18}
						color={iconColor}
						style={styles.iconRight as object}
					/>
				)}
			</View>
			{error && <Text style={styles.errorText as object}>{error}</Text>}
		</View>
	)
}
