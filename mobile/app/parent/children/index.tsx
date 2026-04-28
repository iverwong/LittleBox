import { View, Text, StyleSheet } from 'react-native'

export default function ChildrenIndexScreen() {
  return (
    <View style={styles.container}>
      <Text style={styles.title}>[家长端] 孩子管理</Text>
      <Text style={styles.placeholder}>M2 阶段实现孩子管理功能</Text>
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
  placeholder: {
    fontSize: 16,
    color: '#999',
  },
})
