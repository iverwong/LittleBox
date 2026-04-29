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

export default function LoginScreen() {
  const { setSession, deviceId } = useAuthStore()

  const [phone, setPhone] = useState('')
  const [password, setPassword] = useState('')
  const [phoneError, setPhoneError] = useState('')
  const [passwordError, setPasswordError] = useState('')
  const [isPending, setIsPending] = useState(false)

  const handleLogin = async () => {
    // 前端校验
    let valid = true
    if (!phone.trim()) {
      setPhoneError('请输入手机号')
      valid = false
    } else {
      setPhoneError('')
    }
    if (!password) {
      setPasswordError('请输入密码')
      valid = false
    } else {
      setPasswordError('')
    }
    if (!valid) return

    setIsPending(true)
    try {
      const resp = await api.post<LoginResponse>('/auth/login', {
        phone,
        password,
        device_id: deviceId,
      })

      if (!resp.ok) {
        const status = resp.status
        setPassword('')

        if (status === 401) {
          setPhoneError('账号或密码错误')
          return
        }
        if (status === 429) {
          toast.show({ message: '登录过于频繁，请稍后重试', variant: 'error', duration: 3000 })
          return
        }
        if (status >= 400 && status < 500) {
          toast.show({ message: '登录失败，请检查输入', variant: 'error', duration: 3000 })
          return
        }
        // 5xx：client 已 toast，这里 return 防止进入成功分支
        return
      }

      await setSession({ role: 'parent', token: resp.data.token, userId: resp.data.account.id })
    } catch {
      setPassword('')
      // 网络异常（fetch 抛出的异常不经过 client 5xx 分支）需要兜底 toast
      toast.show({ message: '网络异常，稍后重试', variant: 'error', duration: 3000 })
    } finally {
      setIsPending(false)
    }
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
                label="手机号"
                value={phone}
                onChangeText={(text) => {
                  setPhone(text)
                  if (phoneError) setPhoneError('')
                }}
                placeholder="请输入手机号"
                autoCapitalize="none"
                autoCorrect={false}
                error={phoneError}
                leftIcon="phone"
              />
              <Input
                label="密码"
                value={password}
                onChangeText={(text) => {
                  setPassword(text)
                  if (passwordError) setPasswordError('')
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
              家长账号由管理员创建，请使用收到的手机号和初始密码登录
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
