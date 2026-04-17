import FontAwesome from '@expo/vector-icons/FontAwesome';
import { DarkTheme, DefaultTheme, ThemeProvider } from '@react-navigation/native';
import { useFonts } from 'expo-font';
import { Stack, useRouter, useSegments } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';
import { useEffect } from 'react';
import 'react-native-reanimated';

import { useColorScheme } from '@/components/useColorScheme';
import { useAuthStore } from '../stores/auth';

export {
  ErrorBoundary,
} from 'expo-router';

export const unstable_settings = {
  initialRouteName: '(auth)',
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

  return (
    <ThemeProvider value={colorScheme === 'dark' ? DarkTheme : DefaultTheme}>
      <Stack>
        <Stack.Screen name="(auth)" options={{ headerShown: false }} />
        <Stack.Screen name="(child)" options={{ headerShown: false }} />
        <Stack.Screen name="(parent)" options={{ headerShown: false }} />
      </Stack>
    </ThemeProvider>
  );
}
