import type { StyleProp, ViewStyle } from "react-native";
import type { FeatherName } from "../types";

export type InputSize = "md" | "lg";

export interface InputProps {
  value: string;
  onChangeText: (text: string) => void;
  placeholder?: string;
  label?: string;
  error?: string;
  leftIcon?: FeatherName;
  rightIcon?: FeatherName;
  size?: InputSize;
  secureTextEntry?: boolean;
  keyboardType?: "default" | "email-address" | "numeric" | "phone-pad" | "url";
  autoCapitalize?: "none" | "sentences" | "words" | "characters";
  autoCorrect?: boolean;
  disabled?: boolean;
  maxLength?: number;
  /** 多行输入；true 时高度按 numberOfLines 撑开、文字顶部对齐 */
  multiline?: boolean;
  /** multiline 时可视行数（撑 minHeight），默认 4 */
  numberOfLines?: number;
  /**
   * 是否启用字符计数（需同时传 maxLength）。
   * - < 80% 不显示
   * - [80%, 90%) neutral[300] 温和提示
   * - [90%, 100%) danger 色
   * - = 100% 触发一次快速抖动
   */
  showCount?: boolean;
  onBlur?: () => void;
  style?: StyleProp<ViewStyle>;
}
