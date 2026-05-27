import FontAwesome from '@expo/vector-icons/FontAwesome';
import { DarkTheme, DefaultTheme, ThemeProvider as NavThemeProvider } from '@react-navigation/native';
import { useFonts } from 'expo-font';
import { Stack, useRouter, useSegments, router } from 'expo-router';
import * as SplashScreen from 'expo-splash-screen';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { GestureHandlerRootView } from 'react-native-gesture-handler';
import { useEffect } from 'react';
import { AppState, type AppStateStatus } from 'react-native';
import 'react-native-reanimated';

import { ThemeProvider } from '../theme/ThemeProvider';
import { useColorScheme } from '@/components/useColorScheme';
import { ToastContainer } from '@/components/ui/Toast';
import { useAuthStore } from '../stores/auth';
import { useChatStore } from '../stores/chat';
import { setOn401Handler, setOnUnauthorizedRedirect } from '../services/api/client';

// Module-level injection: runs once when this module is first evaluated (HMR also re-runs).
// This is intentionally placed before any component definition to guarantee the handlers
// are registered before the first API call can happen (even if it somehow precedes hydrate).
setOn401Handler(() => useAuthStore.getState().clearSession());
setOnUnauthorizedRedirect(() => router.replace('/auth/landing' as never));

// Step 9 · AppState 监听支持变量(模块级,避免 React subscribe 噪音)。
// - hadActiveStreamOnBackground:background 时若 activeStreams.size > 0 置 true,abort 所有 stream;
//   active 时若 true,触发 _handleAppStateActive 并清回 false。
// - lastStableAppState:只跟 active / background 两个稳定态;inactive(iOS 过渡态)忽略。
// - 150ms debounce:抹平连续 change 抖动(如 active→inactive→background)。
let hadActiveStreamOnBackground = false;
let lastStableAppState: AppStateStatus =
  AppState.currentState === 'background' ? 'background' : 'active';
let appStateDebounceTimer: ReturnType<typeof setTimeout> | null = null;
const APP_STATE_DEBOUNCE_MS = 150;

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

  // useEffect must be placed after all hooks and before return, ensures hooks call order is stable
  useEffect(() => {
    // Cold-start transient: segments not yet hydrate (useSegments can return
    // [] before router hydrates). Guard against stale fallback redirect that
    // would cover the initial route (START_AT_DEV_HUB → /dev/hub).
    const seg = segments as string[];
    if (seg.length === 0) return;
    if (!hydrated) return;
    const first = seg[0];
    // dev and +not-found routes are exempt from role guard
    if (first === 'dev' || first === '+not-found') return;

    const currentSegment = first
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
      router.replace('/child/chat' as never);
      return;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- expo-router router 引用稳定
  }, [role, segments, hydrated]);

  // Step 9 · AppState 双向监听:background 时关 SSE,active 时拉权威态。
  // 监听器全程挂着(deps=[]),内部按 activeStreams.size + gate flag 自筛触发条件。
  // 不限 role:parent / auth 角色下 activeStreams 必空,自然 no-op。
  useEffect(() => {
    const handleChange = (nextState: AppStateStatus) => {
      // 仅响应 active / background;inactive(iOS 过渡态)忽略
      if (nextState !== 'active' && nextState !== 'background') return;
      if (nextState === lastStableAppState) return;

      // 150ms debounce 收口,抹平 active→inactive→background 连续触发
      if (appStateDebounceTimer) clearTimeout(appStateDebounceTimer);
      appStateDebounceTimer = setTimeout(() => {
        appStateDebounceTimer = null;
        const prev = lastStableAppState;
        // 二次检查:防 debounce 期间状态又变回去(active→inactive→active 不应触发)
        const current = AppState.currentState;
        if (current !== 'active' && current !== 'background') return;
        if (current === prev) return;
        lastStableAppState = current;

        if (prev === 'active' && current === 'background') {
          // 切后台:遍历 activeStreams 主动 abort('backgroundClose')。
          // store._cleanupStream 内 'backgroundClose' 分支仅清 buffer + 删 activeStreams,
          // 保留 status='streaming' / inProgress / streamPhase,等 _handleAppStateActive 接管。
          const streams = useChatStore.getState().activeStreams;
          if (streams.size === 0) return;
          hadActiveStreamOnBackground = true;
          streams.forEach((s) => {
            try {
              s.handle.abort('backgroundClose');
            } catch (e) {
              console.warn('[AppState] backgroundClose abort failed', e);
            }
          });
          console.log('[AppState] → background, aborted streams', {
            count: streams.size,
          });
        } else if (prev === 'background' && current === 'active') {
          // 切前台:gate flag 为真时触发权威态对齐。
          if (!hadActiveStreamOnBackground) return;
          hadActiveStreamOnBackground = false;
          const sid = useChatStore.getState().todaySessionId;
          if (sid == null) {
            console.warn('[AppState] active resume: no todaySessionId, skip');
            return;
          }
          console.log('[AppState] → active, triggering _handleAppStateActive', {
            sid,
          });
          void useChatStore.getState()._handleAppStateActive(sid);
        }
      }, APP_STATE_DEBOUNCE_MS);
    };

    const sub = AppState.addEventListener('change', handleChange);
    return () => {
      sub.remove();
      if (appStateDebounceTimer) {
        clearTimeout(appStateDebounceTimer);
        appStateDebounceTimer = null;
      }
    };
  }, []);

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
