/**
 * parent/children/new.tsx — new child creation form (M5 F4).
 *
 * Fields: nickname (Input) + age (AgePicker) + gender (GenderAvatar × 3)
 * Submit: POST /children { nickname, age, gender }
 *
 * Pre-validation: nickname.trim().length must be in [1, 32] before submit.
 * 422 from backend is a fallback only (schema drift).
 *
 * Success: Toast "已添加" 1.5s + router.back()
 * 409 quota: Toast "最多 3 个孩子，请先删除已有" 3s, stay on form
 * 422: highlight field (nickname error state)
 * Other 4xx: Toast "创建失败，请检查输入" 3s
 * 5xx: Toast "网络异常，稍后重试" 3s
 *
 * Backend contract (schemas/children.py:11-18):
 *   nickname: str (min=1, max=32)
 *   age: int (ge=3, le=21)
 *   gender: "male" | "female" | "unknown"
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
import { GenderAvatar } from '@/components/business/GenderAvatar'

// ---------------------------------------------------------------------------
// Types & constants
// ---------------------------------------------------------------------------

type Gender = 'male' | 'female' | 'unknown'

// Backend constraint (schemas/children.py)
const NICKNAME_MAX = 32

function validateNickname(value: string): string | null {
  const t = value.trim()
  if (t.length < 1) return '请输入昵称'
  if (t.length > NICKNAME_MAX) return `昵称最多 ${NICKNAME_MAX} 个字符`
  return null
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function NewChildScreen() {
  const theme = useTheme()
  const router = useRouter()

  const [nickname, setNickname] = useState('')
  const [nicknameErr, setNicknameErr] = useState<string | null>(null)
  const [age, setAge] = useState(12) // default to mid-teen (补正 3)
  const [gender, setGender] = useState<Gender>('unknown')
  const [submitting, setSubmitting] = useState(false)

  const handleNicknameChange = useCallback((text: string) => {
    setNickname(text)
    // Real-time pre-validation feedback
    const err = validateNickname(text)
    setNicknameErr(err)
  }, [])

  const handleSubmit = useCallback(async () => {
    // Pre-validation before submit
    const trimErr = validateNickname(nickname)
    if (trimErr) {
      setNicknameErr(trimErr)
      return
    }

    setSubmitting(true)

    const res = await api.post<{ id: string }>('/children', {
      nickname: nickname.trim(),
      age,
      gender,
    })

    if (!res.ok) {
      setSubmitting(false)
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

    // Success
    toast.show({ message: '已添加', variant: 'success', duration: 1500 })
    router.back()
  }, [nickname, age, gender, router])

  return (
    <>
      <Stack.Screen options={{ title: '添加孩子' }} />

      <KeyboardAvoidingView
        style={{ flex: 1 }}
        behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
      >
        <ScrollView
          contentContainerStyle={[
            styles.container,
            { backgroundColor: theme.surface.paper },
          ]}
          keyboardShouldPersistTaps="handled"
        >
          {/* ── 昵称 ── */}
          <View style={styles.fieldBlock}>
            <Text style={[styles.label, { color: theme.palette.neutral[700] }]}>
              昵称
            </Text>
            <Input
              value={nickname}
              onChangeText={handleNicknameChange}
              placeholder="请输入孩子昵称"
              error={nicknameErr ?? undefined}
              autoCapitalize="none"
              autoCorrect={false}
              maxLength={NICKNAME_MAX}
            />
          </View>

          {/* ── 年龄 ── */}
          <View style={styles.fieldBlock}>
            <Text style={[styles.label, { color: theme.palette.neutral[700] }]}>
              年龄
            </Text>
            <View style={{ paddingHorizontal: 4 }}>
              <AgePicker value={age} onValueChange={setAge} />
            </View>
          </View>

          {/* ── 性别 ── */}
          <View style={styles.fieldBlock}>
            <Text style={[styles.label, { color: theme.palette.neutral[700] }]}>
              性别
            </Text>
            <View style={styles.genderRow}>
              {(['male', 'female', 'unknown'] as Gender[]).map((g) => (
                <Pressable key={g} onPress={() => setGender(g)}>
                  <GenderAvatar gender={g} size={72} selected={gender === g} />
                </Pressable>
              ))}
            </View>
          </View>

          {/* ── 提交 ── */}
          <View style={styles.submitBlock}>
            <Button
              variant="primary"
              size="lg"
              loading={submitting}
              disabled={submitting}
              onPress={handleSubmit}
              style={{ width: '100%' }}
            >
              保存
            </Button>
          </View>
        </ScrollView>
      </KeyboardAvoidingView>
    </>
  )
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const styles = StyleSheet.create({
  container: {
    padding: 24,
    gap: 28,
  },
  fieldBlock: {
    gap: 8,
  },
  label: {
    fontSize: 14,
    fontWeight: '500',
  },
  genderRow: {
    flexDirection: 'row',
    gap: 20,
    paddingVertical: 4,
  },
  submitBlock: {
    marginTop: 8,
  },
})
