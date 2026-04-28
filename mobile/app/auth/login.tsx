import { View, Text, Pressable, StyleSheet } from 'react-native'
import { useRouter } from 'expo-router'
import { useAuthStore } from '../../stores/auth'

export default function LoginScreen() {
  const router = useRouter()
  const setSession = useAuthStore((state) => state.setSession)

  return (
    <View style={styles.container}>
      <Text style={styles.title}>[家长端] 登录</Text>
      <Text style={styles.subtitle}>手机号 + 验证码登录</Text>

      <Pressable
        style={styles.button}
        onPress={() => {
          setSession({ role: 'parent', token: 'mock-token-parent', userId: 'mock-user-id' })
          router.replace('/parent/children' as never)
        }}
      >
        <Text style={styles.buttonText}>模拟家长登录</Text>
      </Pressable>

      <Pressable
        style={[styles.button, styles.buttonSecondary]}
        onPress={() => {
          setSession({ role: 'child', token: 'mock-token-child', userId: 'mock-user-id' })
          router.replace('/child/welcome' as never)
        }}
      >
        <Text style={[styles.buttonText, styles.buttonTextSecondary]}>
          模拟子端扫码登录
        </Text>
      </Pressable>
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
  },
  title: {
    fontSize: 24,
    fontWeight: 'bold',
    marginBottom: 8,
  },
  subtitle: {
    fontSize: 16,
    color: '#666',
    marginBottom: 32,
  },
  button: {
    backgroundColor: '#007AFF',
    paddingHorizontal: 32,
    paddingVertical: 16,
    borderRadius: 12,
    marginBottom: 16,
    width: '100%',
    alignItems: 'center',
  },
  buttonSecondary: {
    backgroundColor: '#34C759',
  },
  buttonText: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '600',
  },
  buttonTextSecondary: {
    color: '#fff',
  },
})
