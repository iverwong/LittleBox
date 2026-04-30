/**
 * parent/children/new.tsx — 添加孩子表单（M5 F4 重写 v2）。
 *
 * 调整：
 *   - 三个字段统一使用顶部 Text label（昵称/年龄/性别），不再用 Input 自带 label
 *   - 年龄当前值由 DiscreteSlider 的 centerLabel 显示（在 AgePicker 中透传）
 *   - 年龄区域去掉卡片包装，避免视觉拥挤
 *   - 性别三个选项居中分布；选中态额外用底部加粗大字标签强化
 *   - 整页米杏底色铺满，杜绝下半屏黑底
 */
import { useCallback, useState } from 'react'
import {
  View,
  Text,
  StyleSheet,
  ScrollView,
  KeyboardAvoidingView,
  Platform,
  Pressable,
} from 'react-native'
import { Stack, useRouter } from 'expo-router'
import { useTheme } from '@/theme'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { toast } from '@/components/ui/Toast'
import { api } from '@/services/api/client'
import { AgePicker } from '@/components/business/AgePicker'

import { GenderBoy } from '@/components/icons/GenderBoy'
import { GenderGirl } from '@/components/icons/GenderGirl'
import { GenderUnknown } from '@/components/icons/GenderUnknown'

// ---------------------------------------------------------------------------

type Gender = 'male' | 'female' | 'unknown'

const NICKNAME_MAX = 32
const DEFAULT_AGE = 12

const GENDER_OPTIONS: Array<{ value: Gender; label: string }> = [
  { value: 'male', label: '男孩' },
  { value: 'female', label: '女孩' },
  { value: 'unknown', label: '保密' },
]

function validateNickname(value: string): string | null {
  const t = value.trim()
  if (t.length < 1) return '请输入昵称'
  if (t.length > NICKNAME_MAX) return `昵称最多 ${NICKNAME_MAX} 个字`
  return null
}

// ---------------------------------------------------------------------------

export default function NewChildScreen() {
  const theme = useTheme()
  const router = useRouter()

  const [nickname, setNickname] = useState('')
  const [nicknameErr, setNicknameErr] = useState<string | null>(null)
  const [age, setAge] = useState<number>(DEFAULT_AGE)
  const [gender, setGender] = useState<Gender>('unknown')
  const [submitting, setSubmitting] = useState(false)

  const handleNicknameChange = useCallback(
    (text: string) => {
      setNickname(text)
      if (nicknameErr) setNicknameErr(validateNickname(text))
    },
    [nicknameErr],
  )

  const handleSubmit = useCallback(async () => {
    const err = validateNickname(nickname)
    if (err) {
      setNicknameErr(err)
      return
    }
    setSubmitting(true)

    const res = await api.post<{ id: string }>('/children', {
      nickname: nickname.trim(),
      age,
      gender,
    })

    setSubmitting(false)

    if (!res.ok) {
      if (res.status === 409) {
        toast.show({ message: '最多 3 个孩子，请先删除已有', variant: 'error', duration: 3000 })
        return
      }
      if (res.status === 422) {
        setNicknameErr('昵称格式不被接受，请检查输入')
        return
      }
      if (res.status >= 400 && res.status < 500) {
        toast.show({ message: '创建失败，请检查输入', variant: 'error', duration: 3000 })
        return
      }
      toast.show({ message: '网络异常，稍后重试', variant: 'error', duration: 3000 })
      return
    }

    toast.show({ message: '已添加', variant: 'success', duration: 1500 })
    router.back()
  }, [nickname, age, gender, router])

  // 统一的字段标题
  const FieldLabel = ({ children }: { children: string }) => (
    <Text style={[styles.label, { color: theme.palette.neutral[700] }]}>{children}</Text>
  )

  return (
    <>
      <Stack.Screen options={{ title: '添加孩子' }} />
      <View style={[styles.root, { backgroundColor: theme.palette.neutral[50] }]}>
        <KeyboardAvoidingView
          style={styles.flex1}
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        >
          <ScrollView
            contentContainerStyle={styles.scrollContent}
            keyboardShouldPersistTaps="handled"
            showsVerticalScrollIndicator={false}
          >
            {/* 昵称 */}
            <View style={styles.field}>
              <FieldLabel>昵称</FieldLabel>
              <Input
                value={nickname}
                onChangeText={handleNicknameChange}
                placeholder="请输入孩子昵称"
                autoCapitalize="none"
                autoCorrect={false}
                maxLength={NICKNAME_MAX}
                error={nicknameErr ?? undefined}
              />
            </View>

            {/* 年龄 */}
            <View style={styles.field}>
              <FieldLabel>年龄</FieldLabel>
              <AgePicker value={age} onValueChange={setAge} />
            </View>

            {/* 性别 */}
            <View style={styles.genderRow}>
              {GENDER_OPTIONS.map((opt) => {
                const selected = gender === opt.value
                const iconColor = selected
                  ? theme.palette.primary[600]
                  : theme.palette.neutral[500]
                const bgColor = selected
                  ? theme.palette.primary[100]
                  : theme.palette.neutral[100]

                return (
                  <Pressable
                    key={opt.value}
                    onPress={() => setGender(opt.value)}
                    style={styles.genderItem}
                    accessibilityRole="radio"
                    accessibilityState={{ selected }}
                    accessibilityLabel={opt.label}
                  >
                    <View
                      style={[
                        styles.genderIconCircle,
                        {
                          backgroundColor: bgColor,
                          borderColor: selected
                            ? theme.palette.primary[500]
                            : 'transparent',
                        },
                      ]}
                    >
                      {opt.value === 'female' ? (
                        <GenderGirl color={iconColor} size={36} />
                      ) : opt.value === 'male' ?
                        (
                          // male 与 unknown 暂时都用 GenderBoy 占位
                          <GenderBoy color={iconColor} size={36} />
                        ) : (
                          <GenderUnknown color={iconColor} size={36} />
                        )
                      }
                    </View>
                    <Text
                      style={[
                        styles.genderLabel,
                        {
                          color: selected
                            ? theme.palette.primary[600]
                            : theme.palette.neutral[500],
                          fontWeight: selected ? '700' : '500',
                        },
                      ]}
                    >
                      {opt.label}
                    </Text>
                  </Pressable>
                )
              })}
            </View>

            <View style={styles.spacer} />

            {/* 保存 */}
            <Button
              variant="primary"
              size="lg"
              loading={submitting}
              disabled={submitting}
              onPress={handleSubmit}
              style={styles.submit}
            >
              保存
            </Button>
          </ScrollView>
        </KeyboardAvoidingView>
      </View>
    </>
  )
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  flex1: { flex: 1 },
  scrollContent: {
    flexGrow: 1,
    padding: 24,
    paddingBottom: 48,
    gap: 28,
  },
  field: { gap: 10 },
  label: {
    fontSize: 14,
    fontWeight: '500',
  },
  // 性别
  genderRow: {
    flexDirection: 'row',
    justifyContent: 'space-around',
    alignItems: 'flex-start',
    paddingVertical: 8,
  },
  genderItem: {
    alignItems: 'center',
    gap: 8,
    padding: 4,
  },
  genderLabel: {
    fontSize: 14,
  },
  genderIconCircle: {
    width: 64,
    height: 64,
    borderRadius: 32,
    borderWidth: 2,
    alignItems: 'center',
    justifyContent: 'center',
  },
  // 底部
  spacer: { flex: 1, minHeight: 16 },
  submit: { width: '100%' },
})