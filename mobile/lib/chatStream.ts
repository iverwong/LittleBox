/**
 * M7 · SSE 协议解析器 + 流式握手（RN 平台版，react-native-sse）。
 *
 * 历史背景：原版基于 fetch + response.body ReadableStream，但 RN Hermes 默认
 * 不开启流式 fetch（response.body 始终为 null），导致客户端读不到任何 event。
 * 详见偏差记录：见 M7 偏差记录页。
 *
 * 本版改用 react-native-sse（库内部用 XHR + 自行解析 SSE 协议），协议事件 /
 * 状态机 / 首帧超时 / abort / close reason 等上层契约不变。
 */

import EventSource from 'react-native-sse';
import { useAuthStore } from '@/stores/auth';
import { BASE_URL, handle401 } from '@/services/api/client';

export type SseEvent =
  | { type: 'session_meta'; session_id: string; hid: string }
  | { type: 'compression_progress'; stage: 'compressing'; message: string }
  | { type: 'thinking_start' }
  | { type: 'thinking_end' }
  | { type: 'delta'; content: string }
  | { type: 'end'; finish_reason: string; aid: string }
  | { type: 'stopped'; finish_reason: 'user_stopped'; aid?: string }
  | { type: 'error'; message: string; code?: string };

export type ChatStreamCloseReason =
  | 'end'
  | 'stopped'
  | 'error'
  | 'abort'
  | 'firstFrameTimeout';

export type ChatStreamHandle = {
  abort: () => void;
};

export type OpenChatStreamArgs = {
  sid: string | null;
  content: string;
  regenerateFor?: string | null;
  onEvent: (e: SseEvent) => void;
  onClose: (reason: ChatStreamCloseReason) => void;
  firstFrameTimeoutMs?: number;
};

const CHAT_STREAM_PATH = '/me/chat/stream';

type CustomEventName =
  | 'session_meta'
  | 'compression_progress'
  | 'thinking_start'
  | 'thinking_end'
  | 'delta'
  | 'end'
  | 'stopped'
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

  const close = (reason: ChatStreamCloseReason) => {
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
    onClose(reason);
  };

  if (firstFrameTimeoutMs > 0) {
    firstFrameTimer = setTimeout(() => {
      if (closed || firstFrameSeen) return;
      close('firstFrameTimeout');
    }, firstFrameTimeoutMs);
  }

  const { token, deviceId } = useAuthStore.getState();

  if (!deviceId) {
    console.warn('[chatStream] missing deviceId, aborting');
    // 必须延后 close 到下一 tick,避免在 return handle 之前就触发 onClose
    setTimeout(() => close('error'), 0);
    return { abort: () => close('abort') };
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

  es.addEventListener('compression_progress', (event) => {
    if (closed) return;
    const payload = safeParseData((event as { data?: unknown }).data);
    onEvent({
      type: 'compression_progress',
      stage: 'compressing',
      message: typeof payload.message === 'string' ? payload.message : '',
    });
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

    // transport 错误
    console.warn('[chatStream] transport error', {
      xhrStatus: ev.xhrStatus,
      message: ev.message,
      type: ev.type,
    });
    if (ev.xhrStatus === 401) {
      void handle401();
    }
    close('error');
  });

  return {
    abort: () => close('abort'),
  };
}
