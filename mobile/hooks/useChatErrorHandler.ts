/**
 * Step 7 · 聊天错误反馈映射 hook。
 *
 * 三类错误入口统一收口到本 hook：
 * 1. **ApiError**（store action throw：loadSessions / loadMessages / loadMoreMessages）
 *    → 调用方 catch 后调 `handleApiError(e, ctx)` 派发
 * 2. **Stream transport 错误**（chatStream.ts onClose meta.transportStatus）
 *    → stores/chat.ts `_cleanupStream` 经 `setOnChatErrorHandler` 注册的回调派发
 * 3. **stopStream API 失败**（stopSession 4xx/5xx）
 *    → stores/chat.ts `stopStream` 失败分支经同一 callback 派发
 *
 * 状态码 → 行为分发（§3.9 + M7 实施清单）：
 * - 401 → 静默（client.ts handle401 已接管 clearSession + 跳 landing）
 * - 403/404 → toast「会话不可用，已为你切换到新会话」+ setActiveSession(null) + loadSessions(reset)
 *   （MVP 无 createSession 显式端点；下次发消息走 sid=null 路径由后端隐式建 session）
 * - 409 → toast「会话正在响应中…」+ resumeOnEnter(sid)（Step 8 接管；本 Step 占位 log）
 * - 429 → 静默（计划明确不做反馈）
 * - 0 → toast「网络连接异常」（status=0 表示 fetch reject / SSE transport 不可达）
 * - 5xx → toast「服务暂时不可用」
 * - 其它 4xx → 兜底 toast「请求失败 (status)」
 *
 * SSE 业务 error 帧（CompressionError / InternalError）不进本 hook：
 * 按 M7 §5.3，统一进 A4 失败态（Step 6 已落地视觉 + 重新生成 chip）；
 * chatStream.ts 业务 error 帧路径调 `close('error')` **不带 meta**，
 * store `_cleanupStream` 据此判定 meta == null → 跳过 hook 派发。
 */
import { useCallback, useEffect } from 'react';
import { toast } from '@/components/ui';
import { ApiError } from '@/services/api/client';
import {
  setOnChatErrorHandler,
  useChatStore,
  type ChatStreamErrorContext,
} from '@/stores/chat';

export type ApiErrorContext = {
  kind: 'loadSessions' | 'loadMessages' | 'loadMoreMessages';
  sid?: string;
};

type DispatchSource = 'streamTransport' | 'stop' | 'api';

type DispatchContext = {
  sid?: string;
  source: DispatchSource;
};

/**
 * 状态码 → 反馈分发的纯函数；module-level 复用，hook 注册回调和 handleApiError 共调。
 * 副作用：toast.show + useChatStore.getState() 的 store action（均 module-level API，无需 React state）。
 */
function dispatchByStatus(status: number, ctx: DispatchContext): void {
  // 401 已被 client.ts handle401 接管；hook 静默不重复 toast
  if (status === 401) return;

  if (status === 403 || status === 404) {
    toast.show({
      message: '会话不可用，已为你切换到新会话',
      variant: 'warning',
    });
    const store = useChatStore.getState();
    store.setActiveSession(null);
    void store
      .loadSessions({ reset: true })
      .catch((e) =>
        console.warn('[useChatErrorHandler] loadSessions reset failed', e),
      );
    return;
  }

  if (status === 409) {
    toast.show({
      message: '会话正在响应中，正在重新连接…',
      variant: 'info',
    });
    // Step 8 · 409 触发 resumeOnEnter
    // Gate source==='api'：sendMessage POST /me/chat/stream 收到 409 才走这里；
    // SSE transport 不会出 409（SSE 已建连），stop 失败已由 stopStream 自己 abort 兜底
    if (ctx.source === 'api' && ctx.sid) {
      void useChatStore
        .getState()
        .resumeOnEnter(ctx.sid)
        .catch((e) => {
          console.warn('[useChatErrorHandler] 409 → resumeOnEnter failed', e);
        });
    }
    return;
  }

  if (status === 429) {
    // 静默（计划明确不做反馈）
    return;
  }

  if (status === 0) {
    toast.show({
      message: '网络连接异常，请检查后重试',
      variant: 'error',
    });
    return;
  }

  if (status >= 500 && status < 600) {
    toast.show({
      message: '服务暂时不可用，请稍后重试',
      variant: 'error',
    });
    return;
  }

  // 其它 4xx 兜底
  toast.show({
    message: `请求失败 (${status})`,
    variant: 'error',
  });
}

export function useChatErrorHandler() {
  // 注册 store transport error / stop 失败回调；unmount 时清空避免 stale ref
  useEffect(() => {
    const cb = (errorCtx: ChatStreamErrorContext) => {
      dispatchByStatus(errorCtx.status, {
        sid: errorCtx.sid,
        source: errorCtx.source,
      });
    };
    setOnChatErrorHandler(cb);
    return () => {
      setOnChatErrorHandler(null);
    };
  }, []);

  /**
   * ApiError catch 路径分发。
   * 非 ApiError 异常重新抛出，让 ErrorBoundary 接管（保留 React 顶层异常处理纪律）。
   */
  const handleApiError = useCallback(
    (error: unknown, context: ApiErrorContext): void => {
      if (!(error instanceof ApiError)) {
        throw error;
      }
      dispatchByStatus(error.status, {
        sid: context.sid,
        source: 'api',
      });
    },
    [],
  );

  return { handleApiError };
}
