/**
 * BirthdayField — 出生日期选择（米杏自画触发行 + 内联滚轮，跨端一致）。
 * 触发行常驻显示当前日期与「约 N 岁」校准（本地算，不发后端）；
 * 点击展开/收起 quidone 纯 JS 滚轮（年/月/日），取值范围卡 [今天-21y, 今天-3y]，对齐后端 [3,21]。
 */
import { useCallback, useMemo, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { DatePicker } from "@quidone/react-native-wheel-picker";
import { useTheme } from "@/theme";
import { birthDateToAge } from "@/lib/birthDateUtils";

interface BirthdayFieldProps {
  /** 受控值，YYYY-MM-DD */
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
}

function toYMD(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function BirthdayField({
  value,
  onChange,
  disabled = false,
}: BirthdayFieldProps) {
  const theme = useTheme();
  const [open, setOpen] = useState(false);

  // 出生日期可选范围：对齐后端 [3, 21] 岁
  const { minYMD, maxYMD } = useMemo(() => {
    const now = new Date();
    const max = new Date(now.getFullYear() - 3, now.getMonth(), now.getDate()); // 最小 3 岁
    const min = new Date(now.getFullYear() - 21, now.getMonth(), now.getDate()); // 最大 21 岁
    return { minYMD: toYMD(min), maxYMD: toYMD(max) };
  }, []);

  const age = value ? birthDateToAge(value) : null;
  const current = value || maxYMD; // 未选时默认落在「最小 3 岁」当天

  const toggle = useCallback(() => {
    if (!disabled) setOpen((v) => !v);
  }, [disabled]);

  return (
    <View>
      <Pressable
        onPress={toggle}
        disabled={disabled}
        style={({ pressed }) => [
          styles.row,
          {
            borderColor: open
              ? theme.palette.primary[500]
              : theme.palette.neutral[300],
            opacity: disabled ? 0.5 : pressed ? 0.7 : 1,
          },
        ]}
        accessibilityRole="button"
        accessibilityLabel="选择出生日期"
      >
        <Text style={[styles.value, { color: theme.palette.neutral[800] }]}>
          {value || "请选择出生日期"}
        </Text>
        {age != null && (
          <Text style={[styles.age, { color: theme.palette.neutral[300] }]}>
            约 {age} 岁
          </Text>
        )}
      </Pressable>

      {open && (
        <View style={styles.pickerWrap}>
          <DatePicker
            date={current}
            onDateChanged={({ date }) => onChange(date)}
            minDate={minYMD}
            maxDate={maxYMD}
            locale="zh"
            itemTextStyle={{ color: theme.palette.neutral[800], fontSize: 18 }}
          />
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    borderWidth: 1,
    borderRadius: 12,
    paddingHorizontal: 16,
    paddingVertical: 14,
  },
  value: { fontSize: 16 },
  age: { fontSize: 14, fontWeight: "500" },
  pickerWrap: { marginTop: 8, alignItems: "center" },
});
