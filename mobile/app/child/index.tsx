import { View, Text, Pressable, StyleSheet } from 'react-native'
import { useRouter } from 'expo-router'
import { useAuthStore } from '../../stores/auth'

export default function ChildIndexScreen() {
  const router = useRouter()
  const clearSession = useAuthStore((state) => state.clearSession)

  return (
    <View style={styles.container}>
      <Text style={styles.title}>[子端] 会话列表</Text>

      <Pressable style={styles.sessionItem} onPress={() => router.push('/child/chat/1' as never)}>
        <Text style={styles.sessionTitle}>AI 伙伴小明</Text>
        <Text style={styles.sessionPreview}>你好呀！今天想聊什么？</Text>
      </Pressable>

      <Pressable style={styles.sessionItem} onPress={() => router.push('/child/chat/2' as never)}>
        <Text style={styles.sessionTitle}>故事姐姐</Text>
        <Text style={styles.sessionPreview}>今天想听什么故事呀？</Text>
      </Pressable>

      <Pressable style={styles.logoutButton} onPress={clearSession}>
        <Text style={styles.logoutText}>退出登录</Text>
      </Pressable>
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    padding: 16,
  },
  title: {
    fontSize: 24,
    fontWeight: 'bold',
    marginBottom: 24,
  },
  sessionItem: {
    backgroundColor: '#f5f5f5',
    padding: 16,
    borderRadius: 12,
    marginBottom: 12,
  },
  sessionTitle: {
    fontSize: 16,
    fontWeight: '600',
    marginBottom: 4,
  },
  sessionPreview: {
    fontSize: 14,
    color: '#666',
  },
  logoutButton: {
    marginTop: 24,
    padding: 16,
    alignItems: 'center',
  },
  logoutText: {
    color: '#FF3B30',
    fontSize: 16,
  },
})
