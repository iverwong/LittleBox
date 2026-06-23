/**
 * parent/children/[id]/settings.tsx — 孩子配置页（M10 家长配置）。
 * 单页滚动：基础信息 / 关注点 / 敏感度 / 自定义红线。
 * 进入 loading 拉 GET（失败回列表+toast）；改动→右上角保存可点；左上角返回脏则弹确认。
 * 保存成功用 PUT 返回体回填 baseline+draft。视觉范式同 new.tsx：米杏底色 + 自画返回栏。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
  type TextStyle,
  type ViewStyle,
} from "react-native";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { Ionicons } from "@expo/vector-icons";
import { SafeAreaView } from "react-native-safe-area-context";
import { useTheme } from "@/theme";
import { Input } from "@/components/ui/Input";
import { DiscreteSlider } from "@/components/ui/DiscreteSlider";
import { toast } from "@/components/ui/Toast";
import { api } from "@/services/api/client";
import { Endpoints } from "@/constants/endpoints";
import { GenderAvatar } from "@/components/business/GenderAvatar";
import { BirthdayField } from "@/components/business/BirthdayField";
import { UnsavedChangesModal } from "@/components/business/UnsavedChangesModal";
import { birthDateToAge } from "@/lib/birthDateUtils";

type Gender = "male" | "female" | "unknown";

type Sensitivity = {
  emotional: number;
  social: number;
  values: number;
  boundaries: number;
  academic: number;
  lifestyle: number;
};

type ChildProfileDetail = {
  child_user_id: string;
  nickname: string;
  gender: Gender;
  birth_date: string;
  concerns: string | null;
  sensitivity: Sensitivity | null;
  custom_redlines: string | null;
};

const NICKNAME_MAX = 12;
const SENSITIVITY_NODES = [1, 2, 3, 4, 5, 6, 7, 8, 9] as const;
// 关注度 1–9 → 文字档位：镜像后端 audit/prompts.py 的 LEVEL_MAP（单一事实源）。
// 传回后端的是数字，给家长展示的是文字，与审查 prompt 喂给 LLM 的口径一致。
const SENSITIVITY_LEVEL_LABELS: Record<number, string> = {
  1: "完全不关注",
  2: "几乎不关注",
  3: "较少关注",
  4: "略偏宽松",
  5: "正常关注",
  6: "略加留意",
  7: "较为关注",
  8: "高度关注",
  9: "极度关注",
};
const DEFAULT_SENSITIVITY: Sensitivity = {
  emotional: 5,
  social: 5,
  values: 5,
  boundaries: 5,
  academic: 5,
  lifestyle: 5,
};
const SENSITIVITY_DIMS: { key: keyof Sensitivity; label: string }[] = [
  { key: "emotional", label: "情绪与心理" },
  { key: "social", label: "人际与社交" },
  { key: "values", label: "价值观与世界观" },
  { key: "boundaries", label: "AI 应用边界" },
  { key: "academic", label: "学习独立性" },
  { key: "lifestyle", label: "生活方式" },
];
const GENDER_OPTIONS: { value: Gender; label: string }[] = [
  { value: "male", label: "男孩" },
  { value: "female", label: "女孩" },
  { value: "unknown", label: "保密" },
];

type Draft = {
  nickname: string;
  birthDate: string;
  gender: Gender;
  concerns: string;
  sensitivity: Sensitivity;
  customRedlines: string;
};

function toDraft(p: ChildProfileDetail): Draft {
  return {
    nickname: p.nickname,
    birthDate: p.birth_date,
    gender: p.gender,
    concerns: p.concerns ?? "",
    sensitivity: p.sensitivity ?? { ...DEFAULT_SENSITIVITY },
    customRedlines: p.custom_redlines ?? "",
  };
}

function draftEquals(a: Draft, b: Draft): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export default function ChildSettingsScreen() {
  const theme = useTheme();
  const styles = useMemo(() => createChildSettingsStyles(theme), [theme]);
  const router = useRouter();
  const { id } = useLocalSearchParams<{ id: string }>();

  const [loading, setLoading] = useState(true);
  const [baseline, setBaseline] = useState<Draft | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [nicknameErr, setNicknameErr] = useState<string | null>(null);
  const [showLeave, setShowLeave] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      const res = await api.get<ChildProfileDetail>(Endpoints.childProfile(id));
      if (!alive) return;
      if (!res.ok) {
        toast.show({
          message: "加载失败，请重试",
          variant: "error",
          duration: 3000,
        });
        router.back(); // 初始加载失败 → 回子账户列表
        return;
      }
      const d = toDraft(res.data);
      setBaseline(d);
      setDraft(d);
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const dirty = useMemo(
    () => !!baseline && !!draft && !draftEquals(baseline, draft),
    [baseline, draft],
  );

  const patch = useCallback(<K extends keyof Draft>(key: K, val: Draft[K]) => {
    setDraft((prev) => (prev ? { ...prev, [key]: val } : prev));
  }, []);

  const handleBack = useCallback(() => {
    if (dirty) setShowLeave(true);
    else router.back();
  }, [dirty, router]);

  const handleSave = useCallback(async () => {
    if (!draft) return;
    // —— 前端动态校验：镜像后端 Pydantic 约束，仅作即时 UX 反馈，后端为唯一守卫 ——
    const nick = draft.nickname.trim();
    if (nick.length < 1) {
      setNicknameErr("请输入昵称");
      return;
    }
    if (nick.length > NICKNAME_MAX) {
      setNicknameErr(`昵称最多 ${NICKNAME_MAX} 个字`);
      return;
    }
    if (!draft.birthDate) {
      toast.show({
        message: "请选择出生日期",
        variant: "warning",
        duration: 2000,
      });
      return;
    }
    const age = birthDateToAge(draft.birthDate);
    if (age < 3 || age > 21) {
      toast.show({
        message: "出生日期对应年龄需在 3–21 岁之间",
        variant: "warning",
        duration: 2500,
      });
      return;
    }

    setSaving(true);
    // 全量提交：每次携带全部可编辑字段（空串归一为 null），后端整体校验后整体写库
    const res = await api.put<ChildProfileDetail>(Endpoints.childProfile(id), {
      nickname: nick,
      birth_date: draft.birthDate,
      gender: draft.gender,
      sensitivity: draft.sensitivity,
      concerns: draft.concerns.trim() ? draft.concerns.trim() : null,
      custom_redlines: draft.customRedlines.trim()
        ? draft.customRedlines.trim()
        : null,
    });
    setSaving(false);

    if (!res.ok) {
      if (res.status === 422) {
        setNicknameErr("内容格式不被接受，请检查输入");
        return;
      }
      toast.show({
        message: "保存失败，请重试",
        variant: "error",
        duration: 3000,
      });
      return; // 保存失败 → 留在配置页
    }

    const d = toDraft(res.data);
    setBaseline(d);
    setDraft(d);
    setNicknameErr(null);
    toast.show({ message: "已保存", variant: "success", duration: 1500 });
  }, [draft, id]);

  if (loading || !draft) {
    return (
      <>
        <Stack.Screen options={{ headerShown: false }} />
        <SafeAreaView
          style={[styles.root, { backgroundColor: theme.palette.neutral[50] }]}
          edges={["top"]}
        >
          <View style={styles.center}>
            <ActivityIndicator color={theme.palette.primary[500]} />
          </View>
        </SafeAreaView>
      </>
    );
  }

  return (
    <>
      <Stack.Screen options={{ headerShown: false }} />
      <SafeAreaView
        style={[styles.root, { backgroundColor: theme.palette.neutral[50] }]}
        edges={["top"]}
      >
        <View style={styles.topBar}>
          <Pressable
            onPress={handleBack}
            hitSlop={12}
            style={({ pressed }) => [
              styles.topBtn,
              { opacity: pressed ? 0.5 : 1 },
            ]}
            accessibilityRole="button"
            accessibilityLabel="返回"
          >
            <Ionicons
              name="chevron-back"
              size={26}
              color={theme.palette.neutral[800]}
            />
          </Pressable>
          <Text
            style={[styles.topTitle, { color: theme.palette.neutral[800] }]}
          >
            孩子配置
          </Text>
          <Pressable
            onPress={handleSave}
            disabled={!dirty || saving}
            hitSlop={12}
            style={({ pressed }) => [
              styles.topBtn,
              { opacity: !dirty || saving ? 0.4 : pressed ? 0.5 : 1 },
            ]}
            accessibilityRole="button"
            accessibilityLabel="保存"
          >
            {saving ? (
              <ActivityIndicator
                size="small"
                color={theme.palette.primary[500]}
              />
            ) : (
              <Text
                style={[styles.saveText, { color: theme.palette.primary[500] }]}
              >
                保存
              </Text>
            )}
          </Pressable>
        </View>

        <KeyboardAvoidingView
          style={styles.flex1}
          behavior="padding"
          // frame.y 已经包含 TopBar(48) + SafeArea inset 的绝对偏移,
          // 不需要再手动 offset。负值会"欠抬",正值会"多抬留空"。
          keyboardVerticalOffset={0}
        >
          <ScrollView
            contentContainerStyle={styles.scrollContent}
            keyboardShouldPersistTaps="handled"
          // 底部呼吸位由 styles.scrollContent.paddingBottom 提供,
          // 不要再叠 contentInset,否则与 KAV padding 双重补偿,导致
          // "滚到 focused input"目标位算错。
          >
            <Text style={styles.sectionTitle}>基础信息</Text>

            <View style={styles.field}>
              <Text style={styles.label}>昵称</Text>
              <Input
                value={draft.nickname}
                onChangeText={(t) => {
                  patch("nickname", t);
                  if (nicknameErr) setNicknameErr(null);
                }}
                placeholder="给孩子起个称呼"
                maxLength={NICKNAME_MAX}
                showCount
                error={nicknameErr ?? undefined}
              />
            </View>

            <View style={styles.field}>
              <Text style={styles.label}>出生日期</Text>
              <BirthdayField
                value={draft.birthDate}
                onChange={(d) => patch("birthDate", d)}
              />
            </View>

            <View style={styles.field}>
              <Text style={styles.label}>性别</Text>
              <View style={styles.genderRow}>
                {GENDER_OPTIONS.map((opt) => {
                  const selected = draft.gender === opt.value;
                  return (
                    <Pressable
                      key={opt.value}
                      onPress={() => patch("gender", opt.value)}
                      style={styles.genderItem}
                      accessibilityRole="radio"
                      accessibilityState={{ selected }}
                      accessibilityLabel={opt.label}
                    >
                      <View
                        style={[
                          styles.genderIconCircle,
                          {
                            borderColor: selected
                              ? theme.palette.primary[500]
                              : theme.palette.neutral[300],
                          },
                        ]}
                      >
                        <GenderAvatar gender={opt.value} size={56} />
                      </View>
                      <Text
                        style={[
                          styles.genderLabel,
                          {
                            color: selected
                              ? theme.palette.primary[500]
                              : theme.palette.neutral[600],
                            fontWeight: selected ? "700" : "400",
                          },
                        ]}
                      >
                        {opt.label}
                      </Text>
                    </Pressable>
                  );
                })}
              </View>
            </View>

            <View style={styles.divider} />

            <Text style={styles.sectionTitle}>关注点及近况</Text>
            <View style={styles.field}>
              <Text style={styles.label}>希望安全审查多留意的方面（选填）</Text>
              <Input
                value={draft.concerns}
                onChangeText={(t) => patch("concerns", t)}
                placeholder="例如：最近在备考，情绪压力较大"
                multiline
                numberOfLines={4}
                maxLength={500}
                showCount
              />
            </View>

            <Text style={styles.sectionTitle}>红线话题</Text>
            <View style={styles.field}>
              <Text style={styles.label}>额外禁止 / 敏感话题（选填）</Text>
              <Input
                value={draft.customRedlines}
                onChangeText={(t) => patch("customRedlines", t)}
                placeholder="例如：不要谈论某个家庭隐私话题"
                multiline
                numberOfLines={4}
                maxLength={500}
                showCount
              />
            </View>

            <Text style={styles.sectionTitle}>
              关注度（完全不关注 ↔ 极度关注）
            </Text>
            {SENSITIVITY_DIMS.map((dim) => (
              <View key={dim.key} style={styles.field}>
                <Text style={styles.label}>{dim.label}</Text>
                <DiscreteSlider
                  nodes={SENSITIVITY_NODES}
                  value={draft.sensitivity[dim.key]}
                  onValueChange={(v) =>
                    patch("sensitivity", { ...draft.sensitivity, [dim.key]: v })
                  }
                  leftLabel={
                    <Text
                      style={[
                        {
                          fontSize: theme.typography.fontSize.xs,
                          color: theme.palette.neutral[500],
                        },
                      ]}
                    >
                      完全不关注
                    </Text>
                  }
                  rightLabel={
                    <Text
                      style={[
                        {
                          fontSize: theme.typography.fontSize.xs,
                          color: theme.palette.neutral[500],
                        },
                      ]}
                    >
                      极度关注
                    </Text>
                  }
                  centerLabel={(v) => (
                    <Text
                      style={[
                        {
                          fontSize: theme.typography.fontSize.sm,
                          fontWeight: theme.typography.fontWeight.semibold,
                          color: theme.palette.neutral[900],
                        },
                      ]}
                    >
                      {SENSITIVITY_LEVEL_LABELS[v]}
                    </Text>
                  )}
                />
              </View>
            ))}
          </ScrollView>
        </KeyboardAvoidingView>

        <UnsavedChangesModal
          visible={showLeave}
          onCancel={() => setShowLeave(false)}
          onConfirmLeave={() => {
            setShowLeave(false);
            router.back();
          }}
        />
      </SafeAreaView>
    </>
  );
}

type ChildSettingsStyles = {
  root: ViewStyle;
  flex1: ViewStyle;
  center: ViewStyle;
  topBar: ViewStyle;
  topBtn: ViewStyle;
  topTitle: TextStyle;
  saveText: TextStyle;
  scrollContent: ViewStyle;
  sectionTitle: TextStyle;
  field: ViewStyle;
  label: TextStyle;
  genderRow: ViewStyle;
  genderItem: ViewStyle;
  genderIconCircle: ViewStyle;
  genderLabel: TextStyle;
  divider: ViewStyle;
};

const createChildSettingsStyles = (theme: ReturnType<typeof useTheme>) => {
  return StyleSheet.create<ChildSettingsStyles>({
    root: { flex: 1 },
    flex1: { flex: 1 },
    center: { flex: 1, alignItems: "center", justifyContent: "center" },
    topBar: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      paddingHorizontal: 8,
      paddingTop: 4,
      height: 48,
    },
    topBtn: {
      padding: 8,
      minWidth: 44,
      alignItems: "center",
      justifyContent: "center",
    },
    topTitle: { fontSize: 16, fontWeight: "600" },
    saveText: { fontSize: 16, fontWeight: "600" },
    scrollContent: { padding: 24, paddingBottom: 32, gap: 20 },
    sectionTitle: { fontSize: 16, fontWeight: "700", marginTop: 8 },
    field: { gap: 10 },
    label: { fontSize: 14, fontWeight: "500" },
    genderRow: {
      flexDirection: "row",
      justifyContent: "space-around",
      paddingVertical: 4,
    },
    genderItem: { alignItems: "center", gap: 8, padding: 4 },
    genderIconCircle: {
      width: 64,
      height: 64,
      borderRadius: 32,
      borderWidth: 2,
      alignItems: "center",
      justifyContent: "center",
    },
    genderLabel: { fontSize: 14 },
    divider: {
      height: StyleSheet.hairlineWidth,
      backgroundColor: theme.palette.neutral[200],
      marginVertical: 4,
    },
  });
};
