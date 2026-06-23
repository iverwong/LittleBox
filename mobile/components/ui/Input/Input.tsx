import { Feather } from "@expo/vector-icons";
import { TextInput, View, Text } from "react-native";
import { useMemo, useState, useCallback } from "react";
import { useTheme } from "@/theme";
import { createStyles } from "./Input.styles";
import type { InputProps } from "./Input.types";
import type { FeatherName } from "../types";

export function Input({
  value,
  onChangeText,
  placeholder,
  label,
  error,
  leftIcon,
  rightIcon,
  size = "md",
  secureTextEntry = false,
  keyboardType = "default",
  autoCapitalize = "sentences",
  autoCorrect = true,
  disabled = false,
  maxLength,
  multiline = false,
  numberOfLines = 4,
  showCount = false,
  onBlur,
  style,
}: InputProps) {
  const theme = useTheme();
  const styles = useMemo(() => createStyles(theme), [theme]);
  const [focused, setFocused] = useState(false);

  const containerStyle = [
    styles.container,
    focused && styles.container_focused,
    error && styles.container_error,
    disabled && styles.container_disabled,
  ];

  const onFocus = useCallback(() => setFocused(true), []);
  const handleBlur = useCallback(() => {
    setFocused(false);
    onBlur?.();
  }, [onBlur]);

  const iconColor = disabled
    ? theme.palette.neutral[400]
    : theme.palette.neutral[500];
  const textColor = disabled
    ? theme.palette.neutral[500]
    : theme.palette.neutral[800];

  // —— 字符计数：showCount + maxLength 都给才渲染。
  // 阈值(单一门槛 80% + 颜色分级):
  //   < 80%    → 不渲染
  //   [80%,90%) → 中性色(palette.neutral[300],与 BirthdayField 的"约 N 岁"同色)
  //   [90%,100%) → ui.error 红
  //   = 100%   → 红 + "已达限额"
  const showCounter =
    showCount && typeof maxLength === "number" && maxLength > 0;
  const ratio = showCounter ? value.length / maxLength! : 0;
  const counterVariant: "normal" | "warn" | "error" =
    ratio >= 1 ? "error" : ratio >= 0.9 ? "warn" : "normal";
  // < 80% 不渲染(用户感知不到压力时不需要)
  const renderCounter = showCounter && ratio >= 0.8;

  return (
    <View style={[styles.wrap, style]}>
      {label && (
        <Text
          style={{
            fontSize: theme.typography.fontSize.sm,
            color: theme.palette.neutral[700],
            marginBottom: 4,
          }}
        >
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
          maxLength={maxLength}
          onFocus={onFocus}
          onBlur={handleBlur}
          multiline={multiline}
          numberOfLines={multiline ? numberOfLines : undefined}
          style={[
            styles.input,
            size === "lg" ? styles.size_lg : styles.size_md,
            { color: textColor },
            showCounter ? { paddingBottom: 18 } : null,
            multiline && {
              minHeight: numberOfLines * 22,
              textAlignVertical: "top",
            },
          ]}
        />
        {rightIcon && (
          <Feather
            name={rightIcon as FeatherName}
            size={18}
            color={iconColor}
            style={styles.iconRight}
          />
        )}
        {showCounter && (
          <Text
            style={[
              styles.counter,
              counterVariant === "warn" && styles.counterWarn,
              counterVariant === "error" && styles.counterError,
              { opacity: renderCounter ? 1 : 0 },
            ]}
            pointerEvents="none"
            accessibilityLabel={
              ratio >= 1
                ? `已输入 ${value.length} 字，已达上限`
                : `已输入 ${value.length} 字，上限 ${maxLength}`
            }
          >
            {value.length}/{maxLength}
          </Text>
        )}
      </View>
      {error && <Text style={styles.errorText}>{error}</Text>}
    </View>
  );
}
