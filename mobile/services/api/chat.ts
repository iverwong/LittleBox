/**
 * Chat session endpoints (M7 Step 1.1).
 *
 * 4 个 JSON 端点；SSE 端点 (POST /me/chat/stream) 由 lib/chatStream.ts 单独承接，
 * 不在本文件，原因：SSE 不走 ApiResult JSON 兜底语义（response 是 Stream，不是 JSON）。
 *
 * 契约对齐：
 * - backend/app/schemas/sessions.py（请求 / 响应 schema）
 * - backend/app/api/me.py（路径 / 状态码 / decision matrix O）
 */
import { api } from './client';
import type { ApiResult } from './client';

// ---------------------------------------------------------------------------
// types — mirror backend pydantic models
// ---------------------------------------------------------------------------

export type SessionId = string; // uuid 字符串
export type MessageId = string; // uuid 字符串
export type MessageRole = 'human' | 'ai';
export type MessageStatus = 'active' | 'discarded';

/** GET /me/sessions 单项 */
export type SessionListItem = {
  id: SessionId;
  /** schema 是 `str | None`：服务端 today_session_title 可空 */
  title: string | null;
  /** ISO-8601 datetime（Pydantic 默认 ISO 序列化） */
  last_active_at: string;
};

/** GET /me/sessions response */
export type SessionListResponse = {
  /** 不含 today_session_id（服务端已过滤），按 last_active_at desc 排序 */
  sessions: SessionListItem[];
  /** 今日逻辑日 session 的 id；无今日 session 时为 null */
  today_session_id: SessionId | null;
  /** 下一页 cursor；没有更多时为 null */
  next_cursor: string | null;
};

/** GET /me/sessions/{id}/messages 单项（discarded 已被服务端过滤，但 status 字段仍返回，保持类型完整） */
export type MessageListItem = {
  id: MessageId;
  role: MessageRole;
  content: string;
  status: MessageStatus;
  finish_reason: string | null;
  created_at: string;
};

/** GET /me/sessions/{id}/messages response */
export type MessageListResponse = {
  items: MessageListItem[];
  next_cursor: string | null;
  /** Redis chat:lock:{sid} EXISTS 顶层兜底；Redis 失败 → false */
  in_progress: boolean;
};

// ---------------------------------------------------------------------------
// endpoints
// ---------------------------------------------------------------------------

/**
 * GET /me/sessions — 列出当前 child 的活跃 session（不含今日 session，由 today_session_id 单独承载）。
 * keyset 分页：last_active_at desc, id desc；cursor 对客户端不透明。
 */
export function listSessions(params?: {
  cursor?: string;
  /** 1..50，默认 15 */
  limit?: number;
}): Promise<ApiResult<SessionListResponse>> {
  const search = new URLSearchParams();
  if (params?.cursor) search.set('cursor', params.cursor);
  if (params?.limit !== undefined) search.set('limit', String(params.limit));
  const qs = search.toString();
  return api.get<SessionListResponse>(`/me/sessions${qs ? `?${qs}` : ''}`);
}

/**
 * GET /me/sessions/{sid}/messages — 拉取 session 活跃消息。
 * keyset 分页 + 顶层 in_progress（chat:lock:{sid} EXISTS）。
 */
export function getMessages(
  sid: SessionId,
  params?: {
    cursor?: string;
    /** 1..100，默认 50 */
    limit?: number;
  },
): Promise<ApiResult<MessageListResponse>> {
  const search = new URLSearchParams();
  if (params?.cursor) search.set('cursor', params.cursor);
  if (params?.limit !== undefined) search.set('limit', String(params.limit));
  const qs = search.toString();
  return api.get<MessageListResponse>(
    `/me/sessions/${sid}/messages${qs ? `?${qs}` : ''}`,
  );
}

/**
 * POST /me/sessions/{sid}/stop — best-effort 停止；204 返回。
 * 服务端 set running_streams[sid] event；generator 下一次 yield 前退出。
 * 不存在 / 已软删 → 404；非自己的 session → 403。
 */
export function stopSession(sid: SessionId): Promise<ApiResult<null>> {
  return api.post<null>(`/me/sessions/${sid}/stop`, {});
}

/**
 * DELETE /me/sessions/{sid} — 软删（status='deleted'）；204；第二次 → 404（幂等性按 404 兜底）。
 */
export function deleteSession(sid: SessionId): Promise<ApiResult<null>> {
  return api.delete<null>(`/me/sessions/${sid}`);
}
