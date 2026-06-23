/**
 * API endpoint 路径集中定义,与 backend FastAPI 路由装饰器一一对应。
 *
 * 后端 prefix 一览(在 `backend/app/api/*.py` 的 `APIRouter(prefix=...)` 里):
 *   - /api/v1/auth           ->    app/api/auth.py
 *   - /api/v1/bind-tokens    ->    app/api/bind_tokens.py
 *   - /api/v1/me             ->    app/api/me.py
 *   - /api/v1/children       ->    app/api/children.py
 *   - /api/v1/child-profiles ->    app/api/child_profiles.py
 *
 * `/api/v1` 前缀由 `services/api/client.ts` 的 baseURL 拼上,
 * 这里只放 route 路径(后 `@router.xxx("...")` 的字符串)。
 *
 * 约束:任何改后端 path 的 PR,必须同步改这里,避免客户端发到 404。
 * 动态参数一律走函数(参数拼装集中),不用字符串模板散落调用点。
 */

export const Endpoints = {
    // --- auth ---
    authLogin: '/auth/login',
    authLogout: '/auth/logout',

    // --- bind-tokens ---
    bindTokens: '/bind-tokens',
    bindTokenStatus: (bindToken: string) => `/bind-tokens/${bindToken}/status`,
    bindTokenRedeem: (bindToken: string) => `/bind-tokens/${bindToken}/redeem`,

    // --- me ---
    meProfile: '/me/profile',
    meSessions: '/me/sessions',
    meSessionMessages: (sid: string, qs = '') =>
        `/me/sessions/${sid}/messages${qs ? `?${qs}` : ''}`,
    meSessionStop: (sid: string) => `/me/sessions/${sid}/stop`,
    meSession: (sid: string) => `/me/sessions/${sid}`,

    // --- children ---
    children: '/children',
    child: (id: string) => `/children/${id}`,
    childRevokeTokens: (id: string) => `/children/${id}/revoke-tokens`,

    // --- child-profiles ---
    childProfile: (childUserId: string) => `/child-profiles/${childUserId}`,
} as const