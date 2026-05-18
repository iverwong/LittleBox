import { Stack } from 'expo-router'

export default function ChildLayout() {
  return (
    <Stack screenOptions={{ headerShown: false }} >
      <Stack.Screen name="chat/index" />
      <Stack.Screen name="chat/[sessionId]" />
    </Stack>
  )
}