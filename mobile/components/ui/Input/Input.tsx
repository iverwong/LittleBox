import { Feather } from '@expo/vector-icons'
import { TextInput, View, Text } from 'react-native'
import { useMemo, useState, useCallback } from 'react'
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
	autoCapitalize = 'sentences',
	autoCorrect = true,
	disabled = false,
	style,
}: InputProps) {
	const theme = useTheme()
	const styles = useMemo(() => createStyles(theme), [theme])
	const [focused, setFocused] = useState(false)

	const containerStyle = [
		styles.container,
		focused && styles.container_focused,
		error && styles.container_error,
		disabled && styles.container_disabled,
	]

	const onFocus = useCallback(() => setFocused(true), [])
	const onBlur = useCallback(() => setFocused(false), [])

	const iconColor = disabled ? theme.palette.neutral[400] : theme.palette.neutral[500]
	const textColor = disabled ? theme.palette.neutral[500] : theme.palette.neutral[800]

	return (
		<View style={[styles.wrap, style]}>
			{label && (
				<Text style={{ fontSize: theme.typography.fontSize.sm, color: theme.palette.neutral[700], marginBottom: 4 }}>
					{label}
				</Text>
			)}
			<View style={containerStyle} collapsable={false}>
				{leftIcon && (
					<Feather
						name={leftIcon as FeatherName}
						size={18}
						color={iconColor}
						style={styles.iconLeft}
					/>
				)}
				<TextInput
					value={value}
					onChangeText={onChangeText}
					placeholder={placeholder}
					placeholderTextColor={theme.palette.neutral[400]}
					secureTextEntry={secureTextEntry}
					keyboardType={keyboardType}
					autoCapitalize={autoCapitalize}
					autoCorrect={autoCorrect}
					editable={!disabled}
					onFocus={onFocus}
					onBlur={onBlur}
					style={[styles.input, size === 'lg' ? styles.size_lg : styles.size_md, { color: textColor }]}
				/>
				{rightIcon && (
					<Feather
						name={rightIcon as FeatherName}
						size={18}
						color={iconColor}
						style={styles.iconRight}
					/>
				)}
			</View>
			{error && <Text style={styles.errorText}>{error}</Text>}
		</View>
	)
}
