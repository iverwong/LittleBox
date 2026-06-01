/**
 * M7 · SSE 协议解析器 + 流式握手（RN 平台版，react-native-sse）。
 *
 * 历史背景：原版基于 fetch + response.body ReadableStream，但 RN Hermes 默认
 * 不开启流式 fetch（response.body 始终为 null），导致客户端读不到任何 event。
 * 详见偏差记录：见 M7 偏差记录页。
 *
 * 本版改用 react-native-sse（库内部用 XHR + 自行解析 SSE 协议），协议事件 /
 * 状态机 / 首帧超时 / abort / close reason 等上层契约不变。
 *
 * M7-patch · M8-patch 9 事件契约对齐（2026-05）：
 * - `compression_progress` 单事件 → `compression_start` / `compression_end` 双事件
 * - 两个新事件 payload 当前为空 `{}`，类型保持窄定义；未来扩展直接扩 union
 * - 正常时序：session_meta → [compression_start → compression_end → ] thinking_start → ... → end
 * - 失败压缩：session_meta → compression_start → error{code:"CompressionError"}
 * - error.code 枚举：CompressionError / InternalError（解析层不收紧，由 store 做分支）
 */

import EventSource from 'react-native-sse';
import { useAuthStore } from '@/stores/auth';
import { BASE_URL, handle401 } from '@/services/api/client';

export type SseEvent =
  | { type: 'session_meta'; session_id: string; hid: string }
  | { type: 'compression_start' }
  | { type: 'compression_end' }
  | { type: 'thinking_start' }
  | { type: 'thinking_end' }
  | { type: 'delta'; content: string }
  | { type: 'end'; finish_reason: string; aid: string }
  | { type: 'stopped'; finish_reason: 'user_stopped'; aid?: string }
  | { type: 'flow_pause'; reason?: string }
  | { type: 'error'; message: string; code?: string };

export type ChatStreamCloseReason =
  | 'end'
  | 'stopped'
  | 'error'
  | 'abort'
  | 'firstFrameTimeout'
  | 'backgroundClose';

/**
 * Step 7 · close 元信息透传。
 * - transportStatus 仅 transport 错误（HTTP 4xx/5xx / 网络断 / 解析失败）路径带；
 *   业务 error 帧（CompressionError / InternalError）与 end / stopped / abort / firstFrameTimeout
 *   路径不带 meta，调用方据此二分错误来源。
 * - transportStatus === 0 表示网络层不可达（库未上报 xhrStatus），与 client.ts 网络层失败约定一致。
 * - 当前由 store `_cleanupStream` 接收后透传给 useChatErrorHandler（Step 7 第 5 子步），
 *   分发到 toast / createSession / loadMessages / resumeOnEnter（Step 8）等动作。
 */
export type ChatStreamCloseMeta = {
  transportStatus?: number;
};

export type ChatStreamHandle = {
  abort: (reason?: 'abort' | 'backgroundClose') => void;
};

export type OpenChatStreamArgs = {
  sid: string | null;
  content: string;
  regenerateFor?: string | null;
  onEvent: (e: SseEvent) => void;
  onClose: (reason: ChatStreamCloseReason, meta?: ChatStreamCloseMeta) => void;
  firstFrameTimeoutMs?: number;
};

const CHAT_STREAM_PATH = '/me/chat/stream';

type CustomEventName =
  | 'session_meta'
  | 'compression_start'
  | 'compression_end'
  | 'thinking_start'
  | 'thinking_end'
  | 'delta'
  | 'end'
  | 'stopped'
  | 'flow_pause'
  | 'error';

function safeParseData(raw: unknown): Record<string, unknown> {
  if (typeof raw !== 'string' || !raw) return {};
  try {
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return {};
  }
}

export function openChatStream(args: OpenChatStreamArgs): ChatStreamHandle {
  const {
    sid,
    content,
    regenerateFor = null,
    onEvent,
    onClose,
    firstFrameTimeoutMs = 5000,
  } = args;

  let closed = false;
  let firstFrameSeen = false;
  let firstFrameTimer: ReturnType<typeof setTimeout> | null = null;
  let es: EventSource<CustomEventName> | null = null;

  const close = (reason: ChatStreamCloseReason, meta?: ChatStreamCloseMeta) => {
    if (closed) return;
    closed = true;
    if (firstFrameTimer) {
      clearTimeout(firstFrameTimer);
      firstFrameTimer = null;
    }
    try {
      es?.close();
    } catch {
      // ignore
    }
    onClose(reason, meta);
  };

  if (firstFrameTimeoutMs > 0) {
    firstFrameTimer = setTimeout(() => {
      if (closed || firstFrameSeen) return;
      // Step 7 修复 · firstFrameTimeout 同属「后端不可达」语义；
      // react-native-sse 在 connect refused / socket pending 场景下不 emit error event，
      // 全靠 5s 首帧超时收口。带 transportStatus=0（与 xhr transport 错误 status=0 同义），
      // 让 store `_cleanupStream` 透 hook → toast「网络连接异常」。
      close('firstFrameTimeout', { transportStatus: 0 });
    }, firstFrameTimeoutMs);
  }

  const { token, deviceId } = useAuthStore.getState();

  if (!deviceId) {
    console.warn('[chatStream] missing deviceId, aborting');
    // 必须延后 close 到下一 tick,避免在 return handle 之前就触发 onClose
    setTimeout(() => close('error'), 0);
    return { abort: (reason) => close(reason ?? 'abort') };
  }

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
    'X-Device-Id': deviceId,
  };
  if (token) headers.Authorization = `Bearer ${token}`;

  const body = JSON.stringify({
    content,
    session_id: sid,
    regenerate_for: regenerateFor,
  });

  es = new EventSource<CustomEventName>(`${BASE_URL}${CHAT_STREAM_PATH}`, {
    method: 'POST',
    headers,
    body,
    pollingInterval: 0, // 关闭自动重连;Resume 由 store 层 Step 8 控制
  });

  const markFirstFrame = () => {
    if (firstFrameSeen) return;
    firstFrameSeen = true;
    if (firstFrameTimer) {
      clearTimeout(firstFrameTimer);
      firstFrameTimer = null;
    }
  };

  es.addEventListener('session_meta', (event) => {
    if (closed) return;
    markFirstFrame();
    const payload = safeParseData((event as { data?: unknown }).data);
    onEvent({
      type: 'session_meta',
      session_id: String(payload.session_id ?? ''),
      hid: String(payload.hid ?? ''),
    });
  });

  // M7-patch: compression_start / compression_end 双事件（payload 当前为空 {}）
  // 时序：必在 thinking_start 之前；正常压缩两者成对出现，失败压缩 compression_start 后接 error{CompressionError}
  es.addEventListener('compression_start', () => {
    if (closed) return;
    onEvent({ type: 'compression_start' });
  });

  es.addEventListener('compression_end', () => {
    if (closed) return;
    onEvent({ type: 'compression_end' });
  });

  es.addEventListener('thinking_start', () => {
    if (closed) return;
    onEvent({ type: 'thinking_start' });
  });

  es.addEventListener('thinking_end', () => {
    if (closed) return;
    onEvent({ type: 'thinking_end' });
  });

  es.addEventListener('delta', (event) => {
    if (closed) return;
    const payload = safeParseData((event as { data?: unknown }).data);
    onEvent({
      type: 'delta',
      content: typeof payload.content === 'string' ? payload.content : '',
    });
  });

  es.addEventListener('end', (event) => {
    if (closed) return;
    const payload = safeParseData((event as { data?: unknown }).data);
    onEvent({
      type: 'end',
      finish_reason: String(payload.finish_reason ?? ''),
      aid: String(payload.aid ?? ''),
    });
    close('end');
  });

  es.addEventListener('stopped', (event) => {
    if (closed) return;
    const payload = safeParseData((event as { data?: unknown }).data);
    onEvent({
      type: 'stopped',
      finish_reason: 'user_stopped',
      aid: typeof payload.aid === 'string' ? payload.aid : undefined,
    });
    close('stopped');
  });

  // M9.5 · 后端有界队列 overflow 时段二发 flow_pause 帧并关流（段一 headless 跑完 commit②）。
  // 本层只解析透传，不在此 close —— 由 store 决定走 backgroundClose Resume 通道。
  // markFirstFrame：flow_pause 也算后端已响应，避免与首帧超时计时器竞争。
  es.addEventListener('flow_pause', (event) => {
    if (closed) return;
    markFirstFrame();
    const payload = safeParseData((event as { data?: unknown }).data);
    onEvent({
      type: 'flow_pause',
      reason: typeof payload.reason === 'string' ? payload.reason : undefined,
    });
  });

  // 'error' 两类来源:
  // 1) 业务 error 帧(后端 emit event: error,带 data JSON,含 code: CompressionError/InternalError)
  // 2) transport 层错误(HTTP 4xx/5xx / 网络断开 / 解析失败,data 为 null,带 xhrStatus / message)
  es.addEventListener('error', (event) => {
    if (closed) return;
    const ev = event as {
      data?: unknown;
      xhrStatus?: number;
      message?: string;
      type?: string;
    };

    if (typeof ev.data === 'string' && ev.data) {
      // 业务 error 帧
      const payload = safeParseData(ev.data);
      onEvent({
        type: 'error',
        message: typeof payload.message === 'string' ? payload.message : '',
        code: typeof payload.code === 'string' ? payload.code : undefined,
      });
      close('error');
      return;
    }

    // transport 错误（HTTP 4xx/5xx / 网络断 / 解析失败）
    // Step 7 · 透传 xhrStatus 给 onClose meta，由 store → useChatErrorHandler 按状态码分发：
    // - 401：handle401 已接管 clearSession+跳 landing，meta 仍带便于 hook 兜底诊断 / 不重复 toast
    // - 403/404：hook 出「会话不可用」toast + 清 lastSessionId + createSession + loadSessions reset
    // - 409：hook 出「会话正在响应」toast + 触发 resumeOnEnter（Step 8 接管）
    // - 5xx / status==0（网络层不可达）：hook 兜底 toast「服务暂时不可用」
    console.warn('[chatStream] transport error', {
      xhrStatus: ev.xhrStatus,
      message: ev.message,
      type: ev.type,
    });
    if (ev.xhrStatus === 401) {
      void handle401();
    }
    close('error', { transportStatus: ev.xhrStatus ?? 0 });
  });

  return {
    abort: (reason) => close(reason ?? 'abort'),
  };
}
