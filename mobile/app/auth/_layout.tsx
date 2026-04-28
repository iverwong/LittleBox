import { Stack } from 'expo-router'

export default function AuthLayout() {
  return (
    <Stack>
      <Stack.Screen name="login" options={{ title: '登录' }} />
      <Stack.Screen name="scan" options={{ title: '扫码登录' }} />
    </Stack>
  )
}
