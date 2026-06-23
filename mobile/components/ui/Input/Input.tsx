import { Feather } from "@expo/vector-icons";
import { TextInput, View, Text, Animated } from "react-native";
import { useMemo, useState, useCallback, useRef, useEffect } from "react";
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

  // —— 字符计数：showCount + maxLength 都给才启用。
  // 颜色分级：
  //   < 80%    → opacity:0（不可见）
  //   [80%,90%) → neutral[300]
  //   [90%,100%) → ui.error（danger 色）
  //   = 100%    → ui.error + 一次快速抖动
  const showCounter =
    showCount && typeof maxLength === "number" && maxLength > 0;
  const ratio = showCounter ? value.length / maxLength! : 0;
  const counterVariant: "normal" | "warn" | "error" =
    ratio >= 1 ? "error" : ratio >= 0.9 ? "warn" : "normal";
  // < 80% 不渲染(用户感知不到压力时不需要)
  const renderCounter = showCounter && ratio >= 0.8;

  // —— 抖动动画（=100% 瞬间触发一次，6 帧快速衰减，原生线程驱动）——
  const shakeAnim = useRef(new Animated.Value(0)).current;
  const prevRatioRef = useRef(ratio);
  useEffect(() => {
    // 仅在"刚好从 <1 变为 1"的那一刻触发
    if (ratio >= 1 && prevRatioRef.current < 1) {
      shakeAnim.setValue(0);
      Animated.sequence([
        Animated.timing(shakeAnim, {
          toValue: 1,
          duration: 50,
          useNativeDriver: true,
        }),
        Animated.timing(shakeAnim, {
          toValue: -1,
          duration: 50,
          useNativeDriver: true,
        }),
        Animated.timing(shakeAnim, {
          toValue: 0.6,
          duration: 50,
          useNativeDriver: true,
        }),
        Animated.timing(shakeAnim, {
          toValue: -0.6,
          duration: 50,
          useNativeDriver: true,
        }),
        Animated.timing(shakeAnim, {
          toValue: 0.3,
          duration: 50,
          useNativeDriver: true,
        }),
        Animated.timing(shakeAnim, {
          toValue: 0,
          duration: 40,
          useNativeDriver: true,
        }),
      ]).start();
    }
    prevRatioRef.current = ratio;
  }, [ratio]);

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
            renderCounter ? { paddingBottom: 14 } : null,
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
          <Animated.Text
            style={[
              styles.counter,
              counterVariant === "warn" && styles.counterWarn,
              counterVariant === "error" && styles.counterError,
              { opacity: renderCounter ? 1 : 0 },
              {
                transform: [
                  {
                    translateX: shakeAnim.interpolate({
                      inputRange: [-1, 1],
                      outputRange: [-6, 6],
                    }),
                  },
                ],
              },
            ]}
            pointerEvents="none"
            accessibilityLabel={
              ratio >= 1
                ? `已输入 ${value.length} 字，已达上限`
                : `已输入 ${value.length} 字，上限 ${maxLength}`
            }
          >
            {value.length}/{maxLength}
          </Animated.Text>
        )}
      </View>
      {error && <Text style={styles.errorText}>{error}</Text>}
    </View>
  );
}
