import { Redirect } from 'expo-router';

import { useAuthStore } from '../stores/auth';

/**
 * Module-level flag: allows dev tools to switch dev-hub on/off at runtime.
 * Reload (HMR / full refresh) resets to true because the module is re-evaluated.
 * M15: remove alongside Dev Hub deletion.
 */
let START_AT_DEV_HUB = true;
export function setStartAtDevHub(value: boolean): void {
  START_AT_DEV_HUB = value;
}

export default function Index() {
  const { role, hydrated } = useAuthStore();

  if (!hydrated) {
    return null;
  }

  if (__DEV__ && START_AT_DEV_HUB) {
    return <Redirect href={'/dev/hub' as never} />;
  }

  if (!role) {
    return <Redirect href={'/auth/landing' as never} />;
  }
  if (role === 'parent') {
    return <Redirect href={'/parent/children' as never} />;
  }
  if (role === 'child') {
    return <Redirect href={'/child/welcome' as never} />;
  }

  return <Redirect href={'/auth/landing' as never} />;
}