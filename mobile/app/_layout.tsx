import FontAwesome from '@expo/vector-icons/FontAwesome';
import { DarkTheme, DefaultTheme, ThemeProvider as NavThemeProvider } from '@react-navigation/native';
import { useFonts } from 'expo-font';
import { Stack , useRouter, useSegments } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { GestureHandlerRootView } from 'react-native-gesture-handler';
import { useEffect } from 'react';
import 'react-native-reanimated';

import { ThemeProvider } from '../theme/ThemeProvider';
import { useColorScheme } from '@/components/useColorScheme';
import { ToastContainer } from '@/components/ui/Toast';
import { useAuthStore } from '../stores/auth';
import { setOn401Handler, setOnUnauthorizedRedirect } from '../services/api/client';

// Module-level injection: runs once when this module is first evaluated (HMR also re-runs).
// This is intentionally placed before any component definition to guarantee the handlers
// are registered before the first API call can happen (even if it somehow precedes hydrate).
setOn401Handler(() => useAuthStore.getState().clearSession());
setOnUnauthorizedRedirect(() => router.replace('/auth/landing' as never));

export { ErrorBoundary } from 'expo-router';

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
    if (loaded) SplashScreen.hideAsync();
  }, [loaded]);

  // F1: 手动触发 authStore hydrate（替换 F0.5.2 hydrated stub）
  useEffect(() => {
    useAuthStore.getState().hydrate()
  }, [])

  if (!loaded) return null;

  return <RootLayoutNav />;
}

function RootLayoutNav() {
  const colorScheme = useColorScheme();
  const segments = useSegments();
  const router = useRouter();
  const { role, hydrated } = useAuthStore();

  // dev 路由游离于 role guard 外（即使未登录也能访问）
  const isDevRoute = segments[0] === 'dev';

  // useEffect 必须位于所有 hooks 之后、return 之前，确保条件返回不改变 hooks 调用顺序
  useEffect(() => {
    if (!hydrated || isDevRoute) return;

    const currentSegment = segments[0]
    const isAuthGroup = currentSegment === 'auth';
    const isParentGroup = currentSegment === 'parent';
    const isChildGroup = currentSegment === 'child';

    if (role === null && !isAuthGroup) {
      router.replace('/auth/landing' as never);
      return;
    }
    if (role === 'parent' && !isParentGroup) {
      router.replace('/parent/children' as never);
      return;
    }
    if (role === 'child' && !isChildGroup) {
      router.replace('/child/welcome' as never);
      return;
    }
  }, [role, segments, router, isDevRoute, hydrated]);

  // hydrated 过渡态（hooks 已全部调用完毕，此处仅控制渲染）
  if (!hydrated) return null;

  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <ThemeProvider>
        <SafeAreaProvider>
          <ToastContainer />
          <NavThemeProvider value={colorScheme === 'dark' ? DarkTheme : DefaultTheme}>
            <Stack>
              <Stack.Screen name="dev" options={{ headerShown: false }} />
              <Stack.Screen name="auth" options={{ headerShown: false }} />
              <Stack.Screen name="parent" options={{ headerShown: false }} />
              <Stack.Screen name="child" options={{ headerShown: false }} />
            </Stack>
          </NavThemeProvider>
        </SafeAreaProvider>
      </ThemeProvider>
    </GestureHandlerRootView>
  );
}
