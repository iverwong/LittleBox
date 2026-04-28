/**
 * 全局唯一 auth store。
 * 按 F1 闸门 A 必修项 M3：手动 hydrate（非 zustand persist）。
 */
import { create } from 'zustand'
import * as SecureStore from 'expo-secure-store'
import { hydrateFromSecureStore, resetDeviceId, clearSecureStore } from '@/services/api/client'

export type Role = 'parent' | 'child'

interface AuthState {
  role: Role | null  // 字段允许 null（hydrate 前 / clearSession 后）
  token: string | null
  userId: string | null
  deviceId: string | null
  hydrated: boolean
  hydrate: () => Promise<void>
  setSession: (s: { role: Role; token: string; userId: string }) => Promise<void>
  clearSession: () => Promise<void>
  resetDevice: () => Promise<void>
}

export const useAuthStore = create<AuthState>((set) => ({
  role: null,
  token: null,
  userId: null,
  deviceId: null,
  hydrated: false, // F1: 初始 false，RootLayout 触发 hydrate 后置 true

  hydrate: async () => {
    const { token, role, userId, deviceId } = await hydrateFromSecureStore()
    set({
      token,
      role: role as Role,
      userId,
      deviceId,
      hydrated: true,
    })
  },

  setSession: async ({ role, token, userId }) => {
    await Promise.all([
      SecureStore.setItemAsync('auth.token', token),
      SecureStore.setItemAsync('auth.role', role),
      SecureStore.setItemAsync('auth.userId', userId),
    ])
    set({ role, token, userId })
  },

  clearSession: async () => {
    await clearSecureStore()
    set({ role: null, token: null, userId: null, deviceId: null })
  },

  resetDevice: async () => {
    const newDeviceId = await resetDeviceId()
    set({ deviceId: newDeviceId })
  },
}))