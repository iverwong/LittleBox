import { Tabs } from 'expo-router'

export default function ParentLayout() {
  return (
    <Tabs>
      <Tabs.Screen
        name="children"
        options={{
          title: '孩子管理',
          headerShown: true,
        }}
      />
      <Tabs.Screen
        name="notifications"
        options={{
          title: '通知中心',
          headerShown: true,
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: '设置',
          headerShown: true,
        }}
      />
    </Tabs>
  )
}
