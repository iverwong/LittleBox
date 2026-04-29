import { useState } from 'react'
import { View, Text, StyleSheet, KeyboardAvoidingView, Platform, ScrollView } from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'
import { api } from '@/services/api/client'
import { useAuthStore } from '@/stores/auth'
import { toast } from '@/components/ui/Toast/toastStore'
import { Button } from '@/components/ui/Button/Button'
import { Input } from '@/components/ui/Input/Input'

interface LoginResponse {
  token: string
  account: {
    id: string
    phone: string
    role: string
    nickname: string | null
  }
}

// ── 校验 helper（与后端 LoginRequest schema 对齐）─────────────────────
function validatePhone(v: string): string {
  if (v.length < 4) return '用户名至少 4 位'
  if (v.length > 32) return '用户名最多 32 位'
  return ''
}

function validatePassword(v: string): string {
  if (v.length < 8) return '密码至少 8 位'
  if (v.length > 128) return '密码最多 128 位'
  return ''
}

export default function LoginScreen() {
  const { setSession, deviceId } = useAuthStore()

  const [phone, setPhone] = useState('')
  const [password, setPassword] = useState('')
  const [phoneError, setPhoneError] = useState('')
  const [passwordError, setPasswordError] = useState('')
  const [isPending, setIsPending] = useState(false)

  const handleLogin = async () => {
    const trimmedPhone = phone.trim()

    // handleLogin 兜底校验（防用户清空后直接点提交）
    const phoneErr = validatePhone(trimmedPhone)
    const pwdErr = validatePassword(password)
    if (phoneErr || pwdErr) {
      setPhoneError(phoneErr)
      setPasswordError(pwdErr)
      return
    }

    setIsPending(true)
    let resp
    try {
      resp = await api.post<LoginResponse>('/auth/login', {
        phone: trimmedPhone,
        password,
        device_id: deviceId,
      })
    } catch {
      // 网络层错误（DNS / TCP / TLS / fetch reject）
      setPassword('')
      toast.show({ message: '网络异常，稍后重试', variant: 'error', duration: 3000 })
      setIsPending(false)
      return
    }

    if (!resp.ok) {
      setPassword('')

      if (resp.status === 401) {
        setPhoneError('账号或密码错误')
        setIsPending(false)
        return
      }
      if (resp.status === 429) {
        toast.show({ message: '登录过于频繁，请稍后重试', variant: 'error', duration: 3000 })
        setIsPending(false)
        return
      }
      if (resp.status >= 500) {
        toast.show({ message: '网络异常，稍后重试', variant: 'error', duration: 3000 })
        setIsPending(false)
        return
      }
      // 其他 4xx（含 422 / 400 / 403）兜底
      toast.show({ message: '输入格式不正确', variant: 'error', duration: 3000 })
      setIsPending(false)
      return
    }

    await setSession({ role: 'parent', token: resp.data.token, userId: resp.data.account.id })
    setIsPending(false)
  }

  return (
    <SafeAreaView style={styles.container} edges={['bottom']}>
      <KeyboardAvoidingView
        behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
        style={styles.keyboardView}
      >
        <ScrollView
          contentContainerStyle={styles.scrollContent}
          keyboardShouldPersistTaps="handled"
          showsVerticalScrollIndicator={false}
        >
          <View style={styles.form}>
            <Text style={styles.title}>登录</Text>
            <Text style={styles.subtitle}>输入您的账号信息</Text>

            <View style={styles.fields}>
              <Input
                label="用户名"
                value={phone}
                onChangeText={(text) => {
                  setPhone(text)
                  setPhoneError(text.length === 0 ? '' : validatePhone(text))
                }}
                placeholder="请输入用户名"
                autoCapitalize="none"
                autoCorrect={false}
                error={phoneError}
                leftIcon="user"
              />
              <Input
                label="密码"
                value={password}
                onChangeText={(text) => {
                  setPassword(text)
                  setPasswordError(text.length === 0 ? '' : validatePassword(text))
                }}
                placeholder="请输入密码"
                secureTextEntry
                error={passwordError}
                leftIcon="lock"
              />
            </View>

            <Button
              variant="primary"
              size="lg"
              style={styles.loginButton}
              loading={isPending}
              disabled={isPending}
              onPress={handleLogin}
            >
              登录
            </Button>

            <Text style={styles.hint}>
              测试期间家长账号由管理员创建，请使用收到的用户名和初始密码登录
            </Text>
          </View>
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#F2EADF',
  },
  keyboardView: {
    flex: 1,
  },
  scrollContent: {
    flexGrow: 1,
    justifyContent: 'center',
    paddingHorizontal: 24,
    paddingVertical: 32,
  },
  form: {
    width: '100%',
  },
  title: {
    fontSize: 32,
    fontWeight: '700',
    color: '#2B2216',
    marginBottom: 8,
  },
  subtitle: {
    fontSize: 16,
    color: '#7A6546',
    marginBottom: 40,
  },
  fields: {
    gap: 20,
    marginBottom: 32,
  },
  loginButton: {
    width: '100%',
  },
  hint: {
    marginTop: 24,
    fontSize: 13,
    color: '#998260',
    textAlign: 'center',
    lineHeight: 20,
  },
})
