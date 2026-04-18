import FontAwesome from '@expo/vector-icons/FontAwesome';
import { DarkTheme, DefaultTheme, ThemeProvider } from '@react-navigation/native';
import { useFonts } from 'expo-font';
import { Stack } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';
import { useEffect } from 'react';
import 'react-native-reanimated';

import { useColorScheme } from '@/components/useColorScheme';

// [M3-TEMP] 角色守卫相关 import 暂时注释，M3 只测 dev-chat 流式链路，不走登录路径。
// 恢复时机：M4 用户鉴权里程碑实施时一并还原 import 和下方 RootLayoutNav 里的守卫逻辑。
// import { useRouter, useSegments } from 'expo-router';
// import { useAuthStore } from '../stores/auth';

export {
  ErrorBoundary,
} from 'expo-router';

// [M3-TEMP] initialRouteName 由 '(auth)' 改为 'dev-chat'，确保 Expo Go 扫码直达 Demo。
// 恢复时机：M4 实现登录界面后改回 '(auth)'。
export const unstable_settings = {
  initialRouteName: 'dev-chat',
};

SplashScreen.preventAutoHideAsync();

export default function RootLayout() {
  const [loaded, error] = useFonts({
    SpaceMono: require('../assets/fonts/SpaceMono-Regular.ttf'),
    ...FontAwesome.font,
  });

  useEffect(() => {
    if (error) throw error;
  }, [error]);

  useEffect(() => {
    if (loaded) {
      SplashScreen.hideAsync();
    }
  }, [loaded]);

  if (!loaded) {
    return null;
  }

  return <RootLayoutNav />;
}

function RootLayoutNav() {
  const colorScheme = useColorScheme();

  // [M3-TEMP] 角色守卫逻辑整体屏蔽。M3 仅验证流式链路，任意访问都应落在 dev-chat。
  // 恢复时机：M4 用户鉴权里程碑实施时原样还原下方 segments / router / role / useEffect。
  /*
  const segments = useSegments();
  const router = useRouter();
  const { role } = useAuthStore();

  useEffect(() => {
    const currentSegment = segments[0] as string;
    const isAuthGroup = currentSegment === '(auth)';
    const isParentGroup = currentSegment === '(parent)';
    const isChildGroup = currentSegment === '(child)';

    // 未登录：不在 auth 组就进 login
    if (role === null && !isAuthGroup) {
      router.replace('/(auth)/login' as never);
      return;
    }
    // 家长已登录：不在 parent 组才重定向；已在 parent 组内切 Tab 不干预
    if (role === 'parent' && !isParentGroup) {
      router.replace('/(parent)/children' as never);
      return;
    }
    // 子端已登录：不在 child 组才重定向
    if (role === 'child' && !isChildGroup) {
      router.replace('/(child)' as never);
      return;
    }
  }, [role, segments, router]);
  */

  return (
    <ThemeProvider value={colorScheme === 'dark' ? DarkTheme : DefaultTheme}>
      <Stack>
        {/* [M3-TEMP] M3 期间只暴露 dev-chat；其它分组屏幕声明保留注释，M4 还原。 */}
        <Stack.Screen name="dev-chat" options={{ headerShown: false }} />
        {/*
        <Stack.Screen name="(auth)" options={{ headerShown: false }} />
        <Stack.Screen name="(child)" options={{ headerShown: false }} />
        <Stack.Screen name="(parent)" options={{ headerShown: false }} />
        */}
      </Stack>
    </ThemeProvider>
  );
}
