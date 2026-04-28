/**
 * 全局唯一 auth store。
 * 按 F1 闸门 A 必修项 M3：手动 hydrate（非 zustand persist）。
 */
import { create } from 'zustand'
import { hydrateFromSecureStore, resetDeviceId, clearSecureStore } from '@/services/api/client'

export type Role = 'parent' | 'child' | null

interface AuthState {
  role: Role
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
    const SecureStore = await import('expo-secure-store')
    await Promise.all([
      SecureStore.setItemAsync('auth.token', token as string),
      SecureStore.setItemAsync('auth.role', role as string),
      SecureStore.setItemAsync('auth.userId', userId as string),
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