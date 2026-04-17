import { create } from 'zustand'

type Role = 'parent' | 'child' | null

interface AuthState {
  role: Role
  token: string | null
  setAuth: (role: Role, token: string) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  role: null,
  token: null,
  setAuth: (role, token) => set({ role, token }),
  logout: () => set({ role: null, token: null }),
}))
