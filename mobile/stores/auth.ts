import { create } from 'zustand'

type Role = 'parent' | 'child' | null

interface AuthState {
  role: Role
  token: string | null
  hydrated: boolean  // F1: SecureStore hydration complete
  setAuth: (role: Role, token: string) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  role: null,
  token: null,
  hydrated: true,  // stub for F0.5; F1 replaces with SecureStore sync
  setAuth: (role, token) => set({ role, token }),
  logout: () => set({ role: null, token: null, hydrated: true }),
}))
