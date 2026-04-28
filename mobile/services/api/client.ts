/**
 * API client with typed request/response and error handling.
 * 按 §七前端「API client 错误契约」（2026-04-28）落地。
 * 契约来源：M5-plan.md F1 审查意见（重发版）。
 */
import * as SecureStore from 'expo-secure-store'
import { router } from 'expo-router'
import { useAuthStore } from '@/stores/auth'
import { toast } from '@/components/ui/Toast/toastStore'

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; body: unknown }

const BASE_URL = process.env.EXPO_PUBLIC_API_BASE_URL ?? 'http://localhost:8000/api/v1'

if (!process.env.EXPO_PUBLIC_API_BASE_URL && process.env.NODE_ENV === 'production') {
  throw new Error('EXPO_PUBLIC_API_BASE_URL must be set in production')
}

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
 */
export async function ensureDeviceId(): Promise<string> {
  const id = crypto.randomUUID()
  await SecureStore.setItemAsync('auth.deviceId', id)
  return id
}

export async function resetDeviceId(): Promise<string> {
  const id = crypto.randomUUID()
  await SecureStore.setItemAsync('auth.deviceId', id)
  return id
}

export async function clearSecureStore(): Promise<void> {
  await Promise.all([
    SecureStore.deleteItemAsync('auth.token'),
    SecureStore.deleteItemAsync('auth.role'),
    SecureStore.deleteItemAsync('auth.userId'),
    SecureStore.deleteItemAsync('auth.deviceId'),
  ])
}

async function request<T>(method: string, path: string, body?: unknown): Promise<ApiResult<T>> {
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
    const { clearSession } = useAuthStore.getState()
    clearSession()
    router.replace('/auth/landing' as never)
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

export { hydrateFromSecureStore }