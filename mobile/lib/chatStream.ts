/**
 * M7 · SSE 协议解析器 + 流式握手。
 *
 * 消费 M6 + M6-patch3 已锁的 7+1 事件协议：
 *   session_meta → [compression_progress] → [thinking_start → thinking_end] → delta×N → end | stopped | error
 *
 * 职责：transport 层 + 协议解码 + 首帧握手超时。
 * 非职责：Resume、错误反馈映射、buffer 节流、状态机（由 store / hook 接管）。
 *
 * 关键纪律：
 *   - thinking_start / thinking_end payload 是空对象 {}，仅匹配 event.type
 *   - stopped.aid 可缺（StopNoAi 快路径），解构必须可选
 *   - error.code 两枚举：CompressionError / InternalError；M7 内 store 统一进 A4
 *   - 首帧（session_meta）超时默认 5s → onClose('firstFrameTimeout')，由 store.resumeOnTimeout 接管
 *   - body 自然关闭未见终止帧 → onClose('error')，视为协议异常断流
 */

import { useAuthStore } from '@/stores/auth';
import { BASE_URL, ensureDeviceId } from '@/services/api/client';

export type SseEvent =
  | { type: 'session_meta'; session_id: string; hid: string }
  | { type: 'compression_progress'; stage: 'compressing'; message: string }
  | { type: 'thinking_start' }
  | { type: 'thinking_end' }
  | { type: 'delta'; content: string }
  | { type: 'end'; finish_reason: string; aid: string }
  | { type: 'stopped'; finish_reason: 'user_stopped'; aid?: string }
  | { type: 'error'; message: string; code?: string };

/**
 * onClose 回调原因：
 *   - end / stopped / error: 服务端 emit 对应终止帧
 *   - abort: 调用方主动 handle.abort()（用户点停止 / 退出页面）
 *   - firstFrameTimeout: 首帧 session_meta 在 firstFrameTimeoutMs 内未到达
 *
 * Store sendMessage 闭包应当：
 *   - 'firstFrameTimeout' → 调 resumeOnTimeout(sid)（Step 8 实现）
 *   - 'error' 且已见 server error 帧 → 进 A4 失败态（按 SseEvent.error.code 区分文案，M7 内不做）
 *   - 'error' 且未见任何 event → 网络层失败，进 A4 失败态
 *   - 'abort' → 不变更 UI（用户语义）
 */
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
  /** null = 触发后端隐式建 session（首条消息或日切重判后） */
  sid: string | null;
  /** 用户消息内容；regenerate 路径下应为空字符串 */
  content: string;
  /** 仅 regenerate 路径携带，需等于最后一个 active human msg id */
  regenerateFor?: string | null;
  onEvent: (e: SseEvent) => void;
  onClose: (reason: ChatStreamCloseReason) => void;
  /** 首帧（session_meta）握手超时，默认 5000ms；传 0 表示禁用 */
  firstFrameTimeoutMs?: number;
};

const CHAT_STREAM_PATH = '/me/chat/stream';

export function openChatStream(args: OpenChatStreamArgs): ChatStreamHandle {
  const {
    sid,
    content,
    regenerateFor = null,
    onEvent,
    onClose,
    firstFrameTimeoutMs = 5000,
  } = args;

  const controller = new AbortController();
  let closed = false;
  let firstFrameSeen = false;
  let firstFrameTimer: ReturnType<typeof setTimeout> | null = null;

  const close = (reason: ChatStreamCloseReason) => {
    if (closed) return;
    closed = true;
    if (firstFrameTimer) {
      clearTimeout(firstFrameTimer);
      firstFrameTimer = null;
    }
    try {
      controller.abort();
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

  (async () => {
    try {
      const token = useAuthStore.getState().token;
      const deviceId = await ensureDeviceId();

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

      const response = await fetch(`${BASE_URL}${CHAT_STREAM_PATH}`, {
        method: 'POST',
        headers,
        body,
        signal: controller.signal,
      });

      if (!response.ok || !response.body) {
        // 4xx / 5xx：由 store 通过 onClose('error') 后据上下文派生反馈
        // （Step 7 useChatErrorHandler 接 401/429/403/404/409 等）
        // 这里不解析 response body 为 SseEvent，避免与协议 error 帧混淆
        console.warn('[chatStream] non-2xx response', response.status);
        close('error');
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      while (true) {
        if (closed) {
          try {
            await reader.cancel();
          } catch {
            // ignore
          }
          break;
        }

        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        let sepIdx: number;
        while ((sepIdx = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0, sepIdx);
          buffer = buffer.slice(sepIdx + 2);

          const parsed = parseEventBlock(block);
          if (!parsed) continue;

          if (!firstFrameSeen) {
            firstFrameSeen = true;
            if (firstFrameTimer) {
              clearTimeout(firstFrameTimer);
              firstFrameTimer = null;
            }
          }

          onEvent(parsed);

          if (parsed.type === 'end') {
            close('end');
            return;
          }
          if (parsed.type === 'stopped') {
            close('stopped');
            return;
          }
          if (parsed.type === 'error') {
            close('error');
            return;
          }
        }
      }

      // body 自然关闭未见终止帧 → 视为协议异常断流
      if (!closed) close('error');
    } catch (err) {
      if (closed) return;
      if (controller.signal.aborted) {
        close('abort');
      } else {
        console.warn('[chatStream] network error', err);
        close('error');
      }
    }
  })();

  return {
    abort: () => close('abort'),
  };
}

function parseEventBlock(block: string): SseEvent | null {
  let eventName: string | null = null;
  const dataLines: string[] = [];

  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, ''); // CRLF 兼容
    if (!line || line.startsWith(':')) continue; // 空行 / 注释
    const colonIdx = line.indexOf(':');
    if (colonIdx < 0) continue;
    const field = line.slice(0, colonIdx);
    let val = line.slice(colonIdx + 1);
    if (val.startsWith(' ')) val = val.slice(1); // SSE 规范：值首字符空格去掉
    if (field === 'event') eventName = val;
    else if (field === 'data') dataLines.push(val);
  }

  if (!eventName) return null;

  const dataStr = dataLines.join('\n');
  let payload: Record<string, unknown> = {};
  if (dataStr) {
    try {
      payload = JSON.parse(dataStr) as Record<string, unknown>;
    } catch {
      return null;
    }
  }

  switch (eventName) {
    case 'session_meta':
      return {
        type: 'session_meta',
        session_id: String(payload.session_id ?? ''),
        hid: String(payload.hid ?? ''),
      };
    case 'compression_progress':
      return {
        type: 'compression_progress',
        stage: 'compressing',
        message: typeof payload.message === 'string' ? payload.message : '',
      };
    case 'thinking_start':
      return { type: 'thinking_start' };
    case 'thinking_end':
      return { type: 'thinking_end' };
    case 'delta':
      return {
        type: 'delta',
        content: typeof payload.content === 'string' ? payload.content : '',
      };
    case 'end':
      return {
        type: 'end',
        finish_reason: String(payload.finish_reason ?? ''),
        aid: String(payload.aid ?? ''),
      };
    case 'stopped':
      return {
        type: 'stopped',
        finish_reason: 'user_stopped',
        aid: typeof payload.aid === 'string' ? payload.aid : undefined,
      };
    case 'error':
      return {
        type: 'error',
        message: typeof payload.message === 'string' ? payload.message : '',
        code: typeof payload.code === 'string' ? payload.code : undefined,
      };
    default:
      // 未知事件类型安全忽略（协议扩展兼容）
      return null;
  }
}
