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

    const currentSegment = segments[0] as string;
    const isAuthGroup = currentSegment === 'auth';
    const isParentGroup = currentSegment === 'parent';
    const isChildGroup = currentSegment === 'child';

    if (role === null && !isAuthGroup) {
      router.replace('/auth/landing');
      return;
    }
    if (role === 'parent' && !isParentGroup) {
      router.replace('/parent/children');
      return;
    }
    if (role === 'child' && !isChildGroup) {
      router.replace('/child/welcome');
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
