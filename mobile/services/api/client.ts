/**
 * API client with typed request/response and error handling.
 * 按 §七前端「API client 错误契约」（2026-04-28）落地。
 * 契约来源：M5-plan.md F1 审查意见（重发版）。
 */
import * as SecureStore from 'expo-secure-store'
import * as Crypto from 'expo-crypto'
import { toast } from '@/components/ui/Toast/toastStore'

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; body: unknown }

const BASE_URL = (() => {
  const fromEnv = process.env.EXPO_PUBLIC_API_BASE_URL
  if (fromEnv) return fromEnv
  if (__DEV__) return 'http://localhost:8000/api/v1'
  throw new Error('EXPO_PUBLIC_API_BASE_URL must be set in production')
})()

// ---------------------------------------------------------------------------
// 401 callback injection（打破 auth.ts ↔ client.ts 循环依赖）
// ---------------------------------------------------------------------------
let on401Handler: (() => Promise<void>) | null = null
let onUnauthorizedRedirect: (() => void) | null = null

export function setOn401Handler(cb: () => Promise<void>) {
  on401Handler = cb
}

export function setOnUnauthorizedRedirect(cb: () => void) {
  onUnauthorizedRedirect = cb
}

// ---------------------------------------------------------------------------
// SecureStore hydration
// ---------------------------------------------------------------------------

/**
 * 并发从 SecureStore 取 4 个 auth keys。
 * deviceId 缺失时调用 ensureDeviceId 生成并写入。
 */
async function hydrateFromSecureStore(): Promise<{
  token: string | null
  role: string | null
  userId: string | null
  deviceId: string
}> {
  const [token, role, userId, deviceId] = await Promise.all([
    SecureStore.getItemAsync('auth.token'),
    SecureStore.getItemAsync('auth.role'),
    SecureStore.getItemAsync('auth.userId'),
    SecureStore.getItemAsync('auth.deviceId'),
  ])

  let finalDeviceId: string = deviceId ?? (await ensureDeviceId())

  return { token, role, userId, deviceId: finalDeviceId }
}

/**
 * SecureStore 缺失 deviceId 时生成 UUID 并写入。
 * 使用 expo-crypto 而非全局 crypto.randomUUID（Hermes 无此 API）。
 */
export async function ensureDeviceId(): Promise<string> {
  const id = Crypto.randomUUID()
  await SecureStore.setItemAsync('auth.deviceId', id)
  return id
}

export async function resetDeviceId(): Promise<string> {
  const id = Crypto.randomUUID()
  await SecureStore.setItemAsync('auth.deviceId', id)
  return id
}

/**
 * Clears only session-level keys (token, role, userId), preserves deviceId.
 * Used by clearSession (session-only) and resetDevice (full wipe triggers this first).
 */
export async function clearSessionSecureStore(): Promise<void> {
  await Promise.all([
    SecureStore.deleteItemAsync('auth.token'),
    SecureStore.deleteItemAsync('auth.role'),
    SecureStore.deleteItemAsync('auth.userId'),
  ])
}

/**
 * Clears all auth keys including deviceId.
 * Used by resetDevice for full wipe.
 */
export async function clearSecureStore(): Promise<void> {
  await Promise.all([
    SecureStore.deleteItemAsync('auth.token'),
    SecureStore.deleteItemAsync('auth.role'),
    SecureStore.deleteItemAsync('auth.userId'),
    SecureStore.deleteItemAsync('auth.deviceId'),
  ])
}

// ---------------------------------------------------------------------------
// Request dispatcher
// ---------------------------------------------------------------------------

async function request<T>(method: string, path: string, body?: unknown): Promise<ApiResult<T>> {
  // 延迟 import，避免循环依赖
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const { useAuthStore } = require('@/stores/auth')

  const { token, deviceId } = useAuthStore.getState()

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(deviceId ? { 'X-Device-Id': deviceId } : {}),
  }

  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    body: body != null ? JSON.stringify(body) : undefined,
  })

  if (res.ok) {
    const data = await res.json()
    return { ok: true, data }
  }

  if (res.status === 401) {
    if (on401Handler) await on401Handler()
    if (onUnauthorizedRedirect) onUnauthorizedRedirect()
    throw { status: 401 }
  }

  if (res.status === 429) {
    toast.show({ message: '请求过于频繁，请稍后重试', variant: 'error', duration: 3000 })
    throw { status: 429 }
  }

  if (res.status >= 500) {
    toast.show({ message: '网络异常，稍后重试', variant: 'error', duration: 3000 })
    const errBody = await res.json().catch(() => null)
    throw { status: res.status, body: errBody }
  }

  // 其他 4xx（含 409 / 422）：resolve 给调用方处理，不弹 toast
  const errBody = await res.json().catch(() => null)
  return { ok: false, status: res.status, body: errBody }
}

export const api = {
  get<T>(path: string): Promise<ApiResult<T>> {
    return request<T>('GET', path)
  },

  post<T>(path: string, body: unknown): Promise<ApiResult<T>> {
    return request<T>('POST', path, body)
  },

  patch<T>(path: string, body: unknown): Promise<ApiResult<T>> {
    return request<T>('PATCH', path, body)
  },

  delete<T>(path: string): Promise<ApiResult<T>> {
    return request<T>('DELETE', path)
  },
}

export { BASE_URL }
export { hydrateFromSecureStore }
