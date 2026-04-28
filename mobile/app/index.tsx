import { Redirect } from 'expo-router';

import { useAuthStore } from '../stores/auth';

const START_AT_DEV_HUB = true as const;

export default function Index() {
  const { role, hydrated } = useAuthStore();

  if (!hydrated) {
    return null;
  }

  if (__DEV__ && START_AT_DEV_HUB) {
    return <Redirect href="/dev/hub" />;
  }

  if (!role) {
    return <Redirect href="/auth/landing" />;
  }
  if (role === 'parent') {
    return <Redirect href="/parent/children" />;
  }
  if (role === 'child') {
    return <Redirect href="/child/welcome" />;
  }

  return <Redirect href="/auth/landing" />;
}
