import { Stack } from 'expo-router'

export default function AuthLayout() {
  return (
    <Stack>
      <Stack.Screen
        name="landing"
        options={{ headerShown: false }}
      />
      <Stack.Screen
        name="login"
        options={{ title: '登录', headerShown: false }}
      />
      <Stack.Screen
        name="bind/scan"
        options={{ title: '扫码登录', headerShown: false }}
      />
    </Stack>
  )
}
