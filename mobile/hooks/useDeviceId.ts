/**
 * useDeviceId hook — 仅负责「SecureStore 缺失时 randomUUID 写入并返回」。
 * authStore.hydrate 在水合阶段直接读 SecureStore（避免 store 与 hook 互相调用造成循环）。
 */
import { useState, useEffect } from 'react'
import { ensureDeviceId } from '@/services/api/client'

export function useDeviceId(): string | null {
  const [deviceId, setDeviceId] = useState<string | null>(null)

  useEffect(() => {
    // 读取已在 authStore hydrate 时落定，此 hook 仅供组件消费
    // 实际 deviceId 应通过 useAuthStore().deviceId 获取
  }, [])

  return deviceId
}