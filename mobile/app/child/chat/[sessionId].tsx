import { View, Text, StyleSheet } from 'react-native'
import { useLocalSearchParams } from 'expo-router'

export default function ChatScreen() {
  const { sessionId } = useLocalSearchParams()

  return (
    <View style={styles.container}>
      <Text style={styles.title}>[子端] 聊天界面</Text>
      <Text style={styles.sessionId}>Session ID: {sessionId}</Text>
      <Text style={styles.placeholder}>M2 阶段实现聊天功能</Text>
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
    fontSize: 20,
    fontWeight: 'bold',
    marginBottom: 8,
  },
  sessionId: {
    fontSize: 14,
    color: '#666',
    marginBottom: 16,
  },
  placeholder: {
    fontSize: 16,
    color: '#999',
  },
})
