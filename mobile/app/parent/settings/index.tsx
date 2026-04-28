import { View, Text, Pressable, StyleSheet } from 'react-native'
import { useAuthStore } from '../../../stores/auth'

export default function SettingsIndexScreen() {
  const logout = useAuthStore((state) => state.logout)

  return (
    <View style={styles.container}>
      <Text style={styles.title}>[家长端] 设置</Text>

      <Pressable style={styles.logoutButton} onPress={logout}>
        <Text style={styles.logoutText}>退出登录</Text>
      </Pressable>
    </View>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    padding: 24,
  },
  title: {
    fontSize: 20,
    fontWeight: 'bold',
    marginBottom: 24,
  },
  logoutButton: {
    marginTop: 24,
    padding: 16,
    alignItems: 'center',
    backgroundColor: '#f5f5f5',
    borderRadius: 12,
  },
  logoutText: {
    color: '#FF3B30',
    fontSize: 16,
  },
})
