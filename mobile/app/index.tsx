import { Redirect } from 'expo-router';

// [M3-TEMP] 根路由直跳 dev-chat，便于 Expo Go 扫码即进入 Demo。
// 恢复时机：M4 登录界面里程碑实施时，改为根据 role 状态分发到 (auth) / (parent) / (child)。
export default function Index() {
  return <Redirect href="/dev-chat" />;
}
