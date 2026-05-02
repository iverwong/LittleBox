import { router } from 'expo-router';
import { useState } from 'react';

import { toast } from '@/components/ui';
import { api } from '@/services/api/client';
import { useAuthStore } from '@/stores/auth';

type AccountOut = {
  id: string;
  role: 'parent' | 'child';
  family_id: string;
  phone: string | null;
  is_active: boolean;
};

type LoginResponse = {
  token: string;
  account: AccountOut;
};

export function useBindRedeem(options?: {
  onError?: (bindToken: string) => void;
}) {
  const setSession = useAuthStore((s) => s.setSession);
  const deviceId = useAuthStore((s) => s.deviceId);

  const [isPending, setIsPending] = useState(false);

  const redeem = async (bindToken: string) => {
    if (isPending) return;
    setIsPending(true);

    const result = await api.post<LoginResponse>(
      `/bind-tokens/${bindToken}/redeem`,
      { device_id: deviceId },
    );

    if (result.ok) {
      setSession({
        role: 'child',
        token: result.data.token,
        userId: result.data.account.id,
      });
      router.replace('/child/welcome' as never);
      return;
    }

    toast.show({
      message: '绑定码无效或已失效',
      variant: 'error',
      duration: 3000,
    });
    setIsPending(false);
    options?.onError?.(bindToken);
  };

  return { redeem, isPending };
}
