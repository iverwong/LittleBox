import { Stack } from 'expo-router'

export default function ChildLayout() {
  return (
    <Stack>
      <Stack.Screen name="index" options={{ title: '会话列表' }} />
      <Stack.Screen name="chat/[sessionId]" options={{ title: '聊天' }} />
    </Stack>
  )
}
