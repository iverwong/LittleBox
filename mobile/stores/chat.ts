/**
 * M7 · 聊天 store。
 *
 * Step 4a.1 范围（在 Step 1.3 骨架 + Step 2 loadMessages 基础上叠加）：
 * - sendMessage：乐观插入 user + AI 占位 → openChatStream → handle 写入 activeStreams
 * - _onSseEvent：完整 7+1 事件处理（含 session_meta sid migrate + delta 累加 + phase 切换）
 * - _cleanupStream：扩展为 message status 收束（end/stopped/error/abort/firstFrameTimeout）
 *
 * M7-patch · M8-patch 9 事件契约对齐（2026-05）：
 * - SSE union 升级为 9 事件：compression_progress 单事件 → compression_start / compression_end 双事件
 * - compression_start → streamPhase 'compressing'，占位文案 B 案锁定（AIMessage 实现）
 * - compression_end → streamPhase 'feeling'（过渡态），thinking_start 接管切 'thinking'
 * - 占位文案 4 段过渡节奏：feeling → compressing → feeling → thinking
 * - error 帧统一进 A4 失败态（按 M7 §5.3 不按 code 区分 UI 文案）；
 *   失败态视觉 + 重新生成归 Step 6，错误反馈映射归 Step 7
 * - 移除 SessionMessageState.compressionMessage 字段（新契约 payload 为空 {}）
 *
 * 关键纪律：
 * - Map 必须不可变更新（new Map(prev).set(...)）以触发 React re-render
 * - inProgress 仅 UI hint；权威态以 GET messages 顶层 in_progress + 末行 role 为准
 * - token buffer 不进 store（Step 4b 在组件级 ref 实现）
 * - PENDING_SESSION_KEY 为 sid=null 路径的临时桶 key；session_meta 必 migrate 到真 sid
 * - Stream lifecycle：sendMessage 闭包用 ctx.storeKey 持有「当前 store key」，
 *   session_meta migrate 时 _onSseEvent 返回新 key，由 onEvent 闭包更新 ctx
 */

import { create } from 'zustand';
import {
  getMessages,
  listSessions,
  stopSession,
  type MessageListItem,
  type SessionId,
} from '@/services/api/chat';
import {
  openChatStream,
  type SseEvent,
  type ChatStreamHandle,
  type ChatStreamCloseReason,
  type ChatStreamCloseMeta,
} from '@/lib/chatStream';
import { ApiError } from '@/services/api/client';
import { toast } from '@/components/ui';
import {
  dispatchBufferAppend,
  dispatchBufferClear,
  dispatchBufferFlushFinal,
} from '@/lib/streamBuffer';

export type MessageId = string;

export type MessageRole = 'human' | 'ai';

export type MessageStatus =
  | 'committed'
  | 'streaming'
  | 'failed'
  | 'stopped'
  | 'reconnecting' // AppState background→active 期间，等待服务端权威态
  | 'disconnected'; // 底层 fetch 60s 超时，等用户主动点「重新连接」chip

export type StreamPhase =
  | 'idle'
  | 'feeling'
  | 'thinking'
  | 'delta'
  | 'compressing'
  | 'interrupted';

export type Message = {
  id: MessageId; // React key；流式期间为 temp_*，loadMessages 拉回为真实 id
  serverId?: string; // 后端持久化 id（user→session_meta.hid，ai→end.aid / stopped.aid）
  sid: SessionId;
  role: MessageRole;
  content: string;
  status: MessageStatus;
  stoppedTag?: boolean;
  finishReason?: string | null;
  createdAt: string;
};

export type SessionMeta = {
  id: SessionId;
  title: string | null;
  lastActiveAt: string;
};

export type SessionMessageState = {
  messages: Message[];
  hasMore: boolean;
  cursor: string | null;
  lastFetchedAt: number;
  inProgress: boolean;
  streamPhase: StreamPhase;
};

export type ActiveStream = {
  sid: SessionId;
  handle: ChatStreamHandle;
  startedAt: number;
  tempUserId: MessageId;
  tempAiId: MessageId;
  /**
   * Step 6 · 标记本 stream 为「重新生成」流（regenerate_for ≠ null）。
   * _cleanupStream 据此分支 loadMessages 兜底逻辑：
   * - 失败（error / firstFrameTimeout）→ 静默 loadMessages，重建 bucket
   * - 成功（end）/ 用户 stop（stopped / abort）→ 正常终态，不触发 loadMessages
   */
  isRegenerate?: boolean;
};

export type ResumeBranch =
  | { type: 'Active' }
  | { type: 'OK2' }
  | { type: 'Waiting'; pollHandle: number }
  | { type: 'A4Late' };

export type ChatStore = {
  sessions: SessionMeta[];
  sessionsCursor: string | null;
  sessionsHasMore: boolean;

  todaySessionId: SessionId | null;
  activeSessionId: SessionId | null;

  /**
   * Step 7 · A4Late prefill：首帧超时（firstFrameTimeout，session_meta 首帧 5s 未达）后回灌 ChatInput 的用户原文。
   * - 写入：`_cleanupStream('firstFrameTimeout')` 内从 stream.tempUserId 索引 user msg.content
   * - 消费：ChatInput useEffect 监听 prefill 变化，一次性写入 textbox 后调 setPendingPrefill(null) 清空
   * - 语义：null = 无 prefill；非空字符串 = 待消费内容
   */
  pendingPrefill: string | null;

  messagesBySession: Map<SessionId, SessionMessageState>;
  activeStreams: Map<SessionId, ActiveStream>;

  loadSessions: (opts?: { reset?: boolean }) => Promise<void>;
  setActiveSession: (sid: SessionId | null) => void;
  setPendingPrefill: (value: string | null) => void;

  loadMessages: (sid: SessionId) => Promise<void>;
  loadMoreMessages: (sid: SessionId) => Promise<void>;
  sendMessage: (sid: SessionId | null, content: string) => Promise<void>;
  stopStream: (sid: SessionId) => Promise<void>;
  /**
   * Step 6 · A4 失败态点击重新生成（§3.9 方案 C「loading 占位接管」）。
   * Store 内部从 bucket 索引取 failed AI 槽 + orphan human serverId（hid），
   * 调 POST /me/chat/stream 走后端决策矩阵 Row 6（复用孤儿 human）。
   * 调用方（AIMessage）只需传 sid；orphan_hid 不再透传，避免 UI 层维护额外状态。
   */
  regenerate: (sid: SessionId) => Promise<void>;
  resumeOnEnter: (sid: SessionId) => Promise<ResumeBranch>;
  resumeOnTimeout: (sid: SessionId) => Promise<ResumeBranch>;
  deleteSession: (sid: SessionId) => Promise<void>;

  /**
   * SSE event handler.
   * 返回值：session_meta migrate 时返回新 storeKey；其他事件返回 void。
   * 由 sendMessage 闭包通过 ctx.storeKey 持有最新 key，避免 onEvent 闭包失效。
   */
  _onSseEvent: (storeKey: SessionId, event: SseEvent) => SessionId | void;
  _cleanupStream: (
    storeKey: SessionId,
    reason: ChatStreamCloseReason,
    meta?: ChatStreamCloseMeta,
  ) => void;
  /**
   * Step 8 · Resume 三分支决策器（共用入口）。
   * resumeOnEnter / resumeOnTimeout 共用本逻辑；调用方按返回的 ResumeBranch 分发动作。
   */
  _resumeBranchDecide: (
    sid: SessionId,
    ctx: 'enter' | 'timeout',
  ) => Promise<ResumeBranch>;
  /**
   * Step 8 · Waiting 后台 polling 启停。
   * _startResumePolling 内 fabricate / 复用一条 streaming ai 槽 + setInterval 每 2s 决策；
   * 终态（OK2 / A4Late / Active / 超限）由 tick callback 调 _stopResumePolling 收口。
   */
  _startResumePolling: (sid: SessionId, ctx: 'enter' | 'timeout') => number;
  _stopResumePolling: (sid: SessionId) => void;
  /**
   * Step 8 · A4Late fabricate failed ai placeholder。
   * enter 路径：unshift fab_ai_ failed 槽到 bucket.messages[0]（用户能看到 A4 失败卡 + 重新生成 chip）
   * timeout 路径：no-op（_cleanupStream firstFrameTimeout 已把真 ai placeholder 切 failed）
   */
  _fabricateA4LateSlot: (sid: SessionId, ctx: 'enter' | 'timeout') => void;
  /**
   * Step 9 · AppState background→active 接入点。
   * 调用方：app/_layout.tsx AppState 监听器（Step 9 小步 3 接线）。
   * 流程：_startReconnecting → _resumeBranchDecide('enter') → _completeReconnecting / _markDisconnected。
   * fetch 默认 60s 超时（client.ts 不设 AbortController），期间 OS 层 SYN 重试 → 网络恢复就自动可连。
   */
  _handleAppStateActive: (sid: SessionId) => Promise<void>;
  /**
   * Step 9 · 用户点「重新连接」chip 触发（disconnected → reconnecting → 重发决策请求）。
   */
  _retryReconnect: (sid: SessionId) => Promise<void>;
  /**
   * Step 9 · bucket[0] streaming AI 残留槽切 reconnecting 视觉。
   * 仅在 bucket[0] 是 streaming AI 时生效（其他场景无 reconnecting 必要）。
   */
  _startReconnecting: (sid: SessionId) => void;
  /**
   * Step 9 · reconnecting → disconnected（fetch 自然超时或网络层失败时）。
   */
  _markDisconnected: (sid: SessionId) => void;
  /**
   * Step 9 · 决策器返回后按分支切终态：
   * - OK2 → 删 reconnecting 槽 + loadMessages
   * - Waiting → _startResumePolling 接管（reconnecting → streaming，启 polling）
   * - A4Late → reconnecting → failed
   * - Active → 极小概率（race），删 reconnecting 槽让 active 流接管
   */
  _completeReconnecting: (
    sid: SessionId,
    branch: ResumeBranch,
  ) => Promise<void>;
  /**
   * Step 4b · 组件级 50ms flush tick 回写 store.content 的内部 action。
   * 调用方：useStreamBuffer hook 的 onFlush 回调。
   */
  _appendFlushedDelta: (
    storeKey: SessionId,
    aiId: MessageId,
    chunk: string,
  ) => void;
};

/**
 * Step 7 · 聊天错误透 hook 的上下文载体。
 *
 * 两个 store 内部入口共用本类型：
 * - `_cleanupStream` 触发的 stream transport 错误（onClose meta.transportStatus）
 *   → `source: 'streamTransport'`，`reason` 区分 'error' / 'firstFrameTimeout'（当前 hook 仅消费 status，reason 透传）
 * - `stopStream` 失败分支（stopSession 4xx/5xx / 网络断）
 *   → `source: 'stop'`，无 reason
 *
 * status 语义对齐 ApiResult：0=网络层不可达，401/403/404/409/5xx=HTTP 状态码。
 * 业务 error 帧（CompressionError / InternalError）按 M7 §5.3 不进本通道
 *（chatStream.ts onClose 不带 meta → _cleanupStream 跳过派发）。
 */
export type ChatStreamErrorContext = {
  sid: SessionId;
  status: number;
  source: 'streamTransport' | 'stop';
  reason?: ChatStreamCloseReason;
};

/**
 * Module-level callback handler，与 client.ts 的 setOn401Handler / setOnUnauthorizedRedirect 同模式。
 * `useChatErrorHandler` hook 在 mount 时注册、unmount 时清空（避免 stale ref）。
 * 多消费者场景目前不需要（chat 页面唯一 hook 持有者）；将来扩展可改为 listener 数组。
 */
let onChatErrorHandler: ((ctx: ChatStreamErrorContext) => void) | null = null;

export function setOnChatErrorHandler(
  cb: ((ctx: ChatStreamErrorContext) => void) | null,
): void {
  onChatErrorHandler = cb;
}

// sid=null 路径的临时桶 key；session_meta 到来时必 migrate 到真实 sid
export const PENDING_SESSION_KEY: SessionId = '__pending__';

/**
 * Step 8 · Resume Waiting 后台 polling handle 注册表。
 * key=sid，value={ handle }。tick callback 在 await 前后两次比对 map.get(sid)?.handle === handle 防 race。
 */
const resumePollHandles = new Map<
  SessionId,
  { handle: ReturnType<typeof setInterval> }
>();

/** Step 8 · Waiting polling 上限：100 次 × 2s = 200s，超限切 A4Late。 */
const RESUME_POLL_MAX = 100;
const RESUME_POLL_INTERVAL_MS = 2000;

/** Step 8 · fabricate id 前缀；loadMessages 据此 preserve 头部 fabricated 槽不被服务端 items 覆盖。 */
const FAB_ID_PREFIX = 'fab_ai_';

function genFabricateId(): MessageId {
  return `${FAB_ID_PREFIX}${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function genTempId(prefix: 'human' | 'ai'): MessageId {
  return `temp_${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function emptyBucket(): SessionMessageState {
  return {
    messages: [],
    hasMore: false,
    cursor: null,
    lastFetchedAt: Date.now(),
    inProgress: false,
    streamPhase: 'idle',
  };
}

function mapApiMessageToStore(item: MessageListItem, sid: SessionId): Message {
  return {
    id: item.id,
    serverId: item.id,
    sid,
    role: item.role,
    content: item.content,
    status: 'committed',
    finishReason: item.finish_reason,
    createdAt: item.created_at,
  };
}

const notImplemented = (name: string): Promise<never> =>
  Promise.reject(
    new Error(
      `[chatStore] ${name} not implemented yet (Step 1.3 骨架，后续 Step 增量实现)`,
    ),
  );

export const useChatStore = create<ChatStore>((set, get) => ({
  sessions: [],
  sessionsCursor: null,
  sessionsHasMore: false,
  todaySessionId: null,
  activeSessionId: null,
  pendingPrefill: null,
  messagesBySession: new Map(),
  activeStreams: new Map(),

  loadSessions: async (opts) => {
    const reset = opts?.reset ?? false;
    const cursor = reset ? undefined : (get().sessionsCursor ?? undefined);

    const result = await listSessions({ cursor, limit: 15 });

    if (!result.ok) {
      throw new ApiError(result.status, result.body);
    }

    const data = result.data;

    set((state) => {
      const prev = reset ? [] : state.sessions;
      return {
        sessions: [
          ...prev,
          ...data.sessions.map((s) => ({
            id: s.id,
            title: s.title,
            lastActiveAt: s.last_active_at,
          })),
        ],
        sessionsCursor: data.next_cursor,
        sessionsHasMore: data.next_cursor != null,
        todaySessionId: data.today_session_id,
      };
    });
  },

  setActiveSession: (sid) => {
    set({ activeSessionId: sid });
  },

  setPendingPrefill: (value) => {
    set({ pendingPrefill: value });
  },

  loadMessages: async (sid) => {
    const result = await getMessages(sid, { limit: 50 });
    if (!result.ok) {
      throw new ApiError(result.status, result.body);
    }
    const data = result.data;

    set((state) => {
      // Step 8 · preserve 头部 fabricated（fab_ai_*）槽：mount/timeout 路径上 §3.10 useEffect
      // 与 resumeOnEnter/Timeout 并发跑 loadMessages 时，A4Late fabricate 先 prepend 到 [0]，
      // 后到的 loadMessages 默认覆盖整个 messages → fab_ 槽丢失。保留头部 fab_ 槽以维持视觉。
      // streamPhase 同样 preserve：polling 期间 _startResumePolling 已切 'feeling'，
      // 不应被 loadMessages 清回 'idle'。
      const prev = state.messagesBySession.get(sid);
      const headFab = prev?.messages[0]?.id.startsWith(FAB_ID_PREFIX)
        ? prev.messages[0]
        : null;
      const serverMessages = data.items.map((item) =>
        mapApiMessageToStore(item, sid),
      );
      const nextMessages = new Map(state.messagesBySession);
      nextMessages.set(sid, {
        messages: headFab ? [headFab, ...serverMessages] : serverMessages,
        hasMore: data.next_cursor != null,
        cursor: data.next_cursor,
        lastFetchedAt: Date.now(),
        inProgress: data.in_progress,
        streamPhase: prev?.streamPhase ?? 'idle',
      });
      return { messagesBySession: nextMessages };
    });
  },

  loadMoreMessages: async (sid) => {
    const bucket = get().messagesBySession.get(sid);
    if (bucket == null || bucket.cursor == null) {
      return;
    }
    const result = await getMessages(sid, {
      cursor: bucket.cursor,
      limit: 50,
    });
    if (!result.ok) {
      throw new ApiError(result.status, result.body);
    }
    const data = result.data;

    set((state) => {
      const prev = state.messagesBySession.get(sid);
      if (prev == null) return {};
      const nextMessages = new Map(state.messagesBySession);
      nextMessages.set(sid, {
        ...prev,
        messages: [
          ...prev.messages,
          ...data.items.map((item) => mapApiMessageToStore(item, sid)),
        ],
        hasMore: data.next_cursor != null,
        cursor: data.next_cursor,
      });
      return { messagesBySession: nextMessages };
    });
  },

  sendMessage: async (sid, content) => {
    const trimmed = content.trim();
    if (!trimmed) return;

    const initialKey: SessionId = sid ?? PENDING_SESSION_KEY;

    // 同一 storeKey 已有 active stream → 静默 ignore；Step 7 接 409 兜底
    if (get().activeStreams.has(initialKey)) {
      console.warn(
        '[chatStore] sendMessage: stream already active on',
        initialKey,
      );
      return;
    }

    // Step 7 修复 · 清前一条「未达后端」失败对（user.serverId 缺失 = session_meta 未达 = 后端无记录）。
    //
    // 场景：firstFrameTimeout / transport 失败时 session_meta 未达 → 后端 0 记录 → user msg.serverId 始终 undefined。
    // 不清的话 bucket 累积一堆失败 user msg（reload 拉后端时全部消失，前后端 diverge）。
    //
    // 设计语义：用户在 prefill 引导下重发即「重试上一条」，前一条乐观插入条与新条二选一保留即可。
    // 延迟清而非失败立刻清的理由：失败痕迹短期保留让用户感知「上一条没发出去」，立刻清会让 UI 看起来「啥都没发生」。
    //
    // 范围限定：仅清「头部 1 对」（messages[0] AI failed + messages[1] human no serverId）。
    // 历史成功条不动；如果用户连环失败导致头部累积 ≥2 对（不应发生 — 每次 sendMessage 都清），仅清最新 1 对，保守。
    set((prev) => {
      const bucket = prev.messagesBySession.get(initialKey);
      if (!bucket || bucket.messages.length < 2) return {};
      const aiHead = bucket.messages[0];
      if (
        aiHead.role === 'ai' &&
        (aiHead.status === 'failed' || aiHead.status === 'stopped') &&
        aiHead.content.length === 0
      ) {
        const nextMessages = new Map(prev.messagesBySession);
        nextMessages.set(initialKey, {
          ...bucket,
          messages: bucket.messages.slice(2),
        });
        console.log(
          '[chatStore] sendMessage: cleared 1 未达后端 失败对 before retry',
          { storeKey: initialKey },
        );
        return { messagesBySession: nextMessages };
      }
      return {};
    });

    const now = new Date().toISOString();
    const tempUserId = genTempId('human');
    const tempAiId = genTempId('ai');

    const userMsg: Message = {
      id: tempUserId,
      sid: initialKey,
      role: 'human',
      content: trimmed,
      status: 'committed',
      createdAt: now,
    };
    const aiPlaceholder: Message = {
      id: tempAiId,
      sid: initialKey,
      role: 'ai',
      content: '',
      status: 'streaming',
      createdAt: now,
    };

    // 乐观插入（inverted FlatList：messages[0] = newest）
    // PENDING 路径（sid==null）需主动切 activeSessionId/todaySessionId 到 PENDING_SESSION_KEY，
    // 否则 chat/index.tsx 会一直渲染 WelcomeShell（issue 4）。
    // session_meta migrate 时会把这两个字段一并切到真实 sid。
    const isPendingPath = sid == null || sid === PENDING_SESSION_KEY;
    set((prev) => {
      const next = new Map(prev.messagesBySession);
      const bucket = next.get(initialKey) ?? emptyBucket();
      next.set(initialKey, {
        ...bucket,
        messages: [aiPlaceholder, userMsg, ...bucket.messages],
        inProgress: true,
        streamPhase: 'feeling',
        lastFetchedAt: Date.now(),
      });
      return {
        messagesBySession: next,
        ...(isPendingPath && prev.activeSessionId == null
          ? { activeSessionId: PENDING_SESSION_KEY }
          : {}),
        ...(isPendingPath && prev.todaySessionId == null
          ? { todaySessionId: PENDING_SESSION_KEY }
          : {}),
      };
    });

    // 闭包内可变 ctx，承载 session_meta migrate 后的新 storeKey
    const ctx: { storeKey: SessionId } = { storeKey: initialKey };

    const handle = openChatStream({
      sid: isPendingPath ? null : sid,
      content: trimmed,
      onEvent: (event) => {
        const newKey = get()._onSseEvent(ctx.storeKey, event);
        if (newKey) ctx.storeKey = newKey;
      },
      onClose: (reason, meta) => {
        get()._cleanupStream(ctx.storeKey, reason, meta);
      },
    });

    set((prev) => {
      const next = new Map(prev.activeStreams);
      next.set(initialKey, {
        sid: initialKey,
        handle,
        startedAt: Date.now(),
        tempUserId,
        tempAiId,
      });
      return { activeStreams: next };
    });
  },

  stopStream: async (sid) => {
    // Step 5 · 用户点 stop 按钮触发：
    // 1. 调 POST /me/sessions/{sid}/stop（best-effort，204 即视为成功）
    // 2. 成功 → 等服务端 SSE 'stopped' 事件自然到达（generator 下一次 yield 前退出 → emit stopped → close）
    //    onEvent('stopped') 精确写 status/stoppedTag；onClose('stopped') 触发 _cleanupStream 收尾
    // 3. 失败（404/403/5xx/网络）→ 服务端无法发 stopped 帧，前端主动 abort handle 触发 onClose('abort') 兜底
    //
    // 防御性 race：sid 已无 active stream（已自然 end / 已被另一处 cleanup）→ 静默返回
    const stream = get().activeStreams.get(sid);
    if (!stream) {
      console.warn('[chatStore] stopStream: no active stream on', sid);
      return;
    }

    // PENDING 哨兵：session_meta 未到达，后端尚未建立该 session，'__pending__' 不是合法 UUID。
    // 绝不能拿它调 stopSession（会触发后端 asyncpg DataError）。本地 abort handle 即可：
    // onClose('abort') → _cleanupStream 收尾，等价于上面失败兜底分支（后端本就无可停）。
    if (sid === PENDING_SESSION_KEY) {
      console.warn(
        '[chatStore] stopStream: pending session, aborting locally without backend stop',
      );
      stream.handle.abort();
      return;
    }

    const result = await stopSession(sid);
    if (!result.ok) {
      console.warn('[chatStore] stopStream: stop API failed, aborting handle', {
        sid,
        status: result.status,
      });
      // Step 7 · 透 hook 派发：401 静默 / 403/404 切 session / 5xx toast。
      // 派发与 abort 顺序无依赖：派发是同步调 toast / setState；abort 触发 onClose('abort') 走 _cleanupStream 链路。
      if (onChatErrorHandler) {
        onChatErrorHandler({
          sid,
          status: result.status,
          source: 'stop',
        });
      }
      stream.handle.abort();
    }
    // 成功路径不在此处做 cleanup；服务端 stopped 帧到达后由 onEvent + onClose 串行触发
  },
  regenerate: async (sid) => {
    // Step 6 · §3.9 方案 C「loading 占位接管」：
    // 1. 从 bucket 取 failed AI 槽（messages[0]）+ orphan human serverId（messages[1].serverId）
    //    bucket 是 inverted（newest at 0），所以 messages[1] 一定是 failed AI 对应的 human 行
    // 2. 复用 failed AI 槽（不删不增）：重置 status='streaming' + content='' + 清 stoppedTag
    //    → StreamingPlaceholder 自动接管渲染「思考中…」
    // 3. 调 openChatStream 带 regenerateFor=orphan_hid（后端 Row 6 复用孤儿，content 必须 ""）
    // 4. 注册 ActiveStream 标记 isRegenerate=true，复用现有 tempAiId（SSE 事件命中同一槽）
    //
    // 失败处理：_cleanupStream('error'|'firstFrameTimeout') + isRegenerate=true → 静默 loadMessages 兜底
    // 成功处理：_cleanupStream('end') 正常 committed，serverId 更新为新 aid

    if (get().activeStreams.has(sid)) {
      console.warn('[chatStore] regenerate: stream already active on', sid);
      return;
    }

    const bucket = get().messagesBySession.get(sid);
    if (!bucket || bucket.messages.length < 2) {
      console.warn('[chatStore] regenerate: bucket too short', sid);
      return;
    }
    const failedAi = bucket.messages[0];
    const orphanHuman = bucket.messages[1];
    // status 二分允许 'failed' | 'stopped'：
    // - failed：A4 失败态 chip（原路径）
    // - stopped：thinking 阶段被 stop / abort 兜底无内容（chip 语义 = 重发）
    // 二者点 chip 后行为完全相同：复用 orphan human 走后端 Row 6 重跑 prompt。
    if (
      failedAi.role !== 'ai' ||
      (failedAi.status !== 'failed' && failedAi.status !== 'stopped') ||
      orphanHuman.role !== 'human' ||
      !orphanHuman.serverId
    ) {
      // 不变式破坏：firstFrameTimeout / transport 失败时 session_meta 未达 → user.serverId 缺失，
      // 后端 Row 6「复用孤儿 human」必须有 hid，缺失时 regenerate 这条路走不通。
      // 用户应改走「直接重发」（pendingPrefill 已回填 ChatInput），toast 引导。
      console.warn('[chatStore] regenerate: invariant violated', {
        sid,
        failedAiRole: failedAi.role,
        failedAiStatus: failedAi.status,
        orphanHumanRole: orphanHuman.role,
        hasOrphanServerId: !!orphanHuman.serverId,
      });
      toast.show({
        message: '网络异常导致请求未发出，请直接重新发送（已为你回填内容）',
        variant: 'warning',
      });
      return;
    }

    const aiTempId = failedAi.id;
    const orphanHid = orphanHuman.serverId;

    // 复用失败 AI 槽 → 流式占位
    set((state) => {
      const cur = state.messagesBySession.get(sid);
      if (!cur) return {};
      const next = new Map(state.messagesBySession);
      next.set(sid, {
        ...cur,
        messages: cur.messages.map((m) =>
          m.id === aiTempId
            ? {
                ...m,
                content: '',
                status: 'streaming',
                stoppedTag: undefined,
                finishReason: undefined,
              }
            : m,
        ),
        inProgress: true,
        streamPhase: 'feeling',
      });
      return { messagesBySession: next };
    });

    // session_meta 不会 migrate（regenerate 必有真实 sid + orphan hid 已存在）；
    // 但保留 ctx 模式与 sendMessage 对称，便于未来扩展。
    const ctx: { storeKey: SessionId } = { storeKey: sid };

    const handle = openChatStream({
      sid,
      content: '',
      regenerateFor: orphanHid,
      onEvent: (event) => {
        const newKey = get()._onSseEvent(ctx.storeKey, event);
        if (newKey) ctx.storeKey = newKey;
      },
      onClose: (reason, meta) => {
        get()._cleanupStream(ctx.storeKey, reason, meta);
      },
    });

    set((prev) => {
      const next = new Map(prev.activeStreams);
      next.set(sid, {
        sid,
        handle,
        startedAt: Date.now(),
        tempUserId: orphanHuman.id, // 复用现有 human id，session_meta hid 回填幂等
        tempAiId: aiTempId,
        isRegenerate: true,
      });
      return { activeStreams: next };
    });
  },
  resumeOnEnter: async (sid) => {
    // mount 路径决策器入口。
    // OK2 由 §3.10 useEffect 兜底装 bucket（不在此处调 loadMessages 避免双拉）；
    // Waiting → _startResumePolling 启 polling + fabricate streaming；
    // A4Late → _fabricateA4LateSlot unshift failed 槽（用户能点重新生成 chip）。
    const branch = await get()._resumeBranchDecide(sid, 'enter');
    if (branch.type === 'Waiting') {
      const handle = get()._startResumePolling(sid, 'enter');
      return { type: 'Waiting', pollHandle: handle };
    }
    if (branch.type === 'A4Late') {
      get()._fabricateA4LateSlot(sid, 'enter');
    }
    return branch;
  },
  resumeOnTimeout: async (sid) => {
    // firstFrameTimeout 自纠错路径。_cleanupStream 已切 ai placeholder 'failed' + 写 pendingPrefill；
    // 决策器若纠为 OK2 → 拉权威态覆盖失败槽；
    // 若纠为 Waiting → _startResumePolling 内复用 bucket[0] 的 failed ai 改回 streaming + 清 pendingPrefill；
    // 若仍 A4Late → no-op（_cleanupStream 已成型 A4，timeout 路径 _fabricateA4LateSlot 内部短路）。
    const branch = await get()._resumeBranchDecide(sid, 'timeout');
    if (branch.type === 'Waiting') {
      const handle = get()._startResumePolling(sid, 'timeout');
      return { type: 'Waiting', pollHandle: handle };
    }
    if (branch.type === 'OK2') {
      try {
        await get().loadMessages(sid);
      } catch (e) {
        console.warn('[chatStore] resumeOnTimeout OK2 loadMessages failed', e);
      }
    }
    return branch;
  },
  deleteSession: (sid) => {
    void sid;
    return notImplemented('deleteSession');
  },

  _resumeBranchDecide: async (sid, ctx) => {
    // Step 8 · Resume 三分支决策器。决策顺序：
    // 1. 已有 active stream → Active（防御并发，让现有流接管）
    // 2. PENDING_SESSION_KEY → A4Late（无 sid 无法查后端；enter 路径 console.warn）
    // 3. GET messages 失败 → throw ApiError（上游 useChatErrorHandler 兜底）
    // 4. in_progress=true → Waiting（Redis lock 仍存在，后端真在跑）
    // 5. items 空 + idle → OK2（空 session）
    // 6. items[0].role='ai'（newest first）→ OK2（上一轮已 commit）
    // 7. items[0].role='human' → A4Late（孤儿 human，上次跑挂了）
    if (get().activeStreams.has(sid)) {
      return { type: 'Active' };
    }
    if (sid === PENDING_SESSION_KEY) {
      if (ctx === 'enter') {
        console.warn(
          '[chatStore] _resumeBranchDecide: PENDING key on enter path (unexpected)',
        );
      }
      return { type: 'A4Late' };
    }

    const result = await getMessages(sid, { limit: 1 });
    if (!result.ok) {
      throw new ApiError(result.status, result.body);
    }
    const { items, in_progress } = result.data;

    if (in_progress) {
      return { type: 'Waiting', pollHandle: -1 };
    }
    if (items.length === 0) {
      return { type: 'OK2' };
    }
    if (items[0].role === 'ai') {
      return { type: 'OK2' };
    }
    return { type: 'A4Late' };
  },

  _fabricateA4LateSlot: (sid, ctx) => {
    // timeout 路径：no-op —— _cleanupStream firstFrameTimeout 已把 sendMessage 的真 ai placeholder 切 failed。
    if (ctx === 'timeout') return;

    set((state) => {
      const bucket = state.messagesBySession.get(sid) ?? emptyBucket();
      const head = bucket.messages[0];
      // 幂等：bucket[0] 已是 fab_ failed 槽 → 不重复 prepend
      if (head?.id.startsWith(FAB_ID_PREFIX) && head.status === 'failed') {
        return {};
      }
      const fabSlot: Message = {
        id: genFabricateId(),
        sid,
        role: 'ai',
        content: '',
        status: 'failed',
        createdAt: new Date().toISOString(),
      };
      const next = new Map(state.messagesBySession);
      next.set(sid, {
        ...bucket,
        messages: [fabSlot, ...bucket.messages],
        inProgress: false,
        streamPhase: 'idle',
      });
      return { messagesBySession: next };
    });
  },

  _startResumePolling: (sid, ctx) => {
    // 防重入：同 sid 已有 polling → clear 旧的
    const existing = resumePollHandles.get(sid);
    if (existing) {
      clearInterval(existing.handle);
      resumePollHandles.delete(sid);
    }

    // 视觉占位：bucket[0] 二分判定
    // - timeout 路径 + bucket[0] = 真 ai failed（非 fab_ 前缀）→ 复用：切回 streaming + 清 content/stoppedTag
    // - bucket[0] 已是 fab_ 前缀 → 改 status 'streaming' + 清 content（幂等覆盖）
    // - 其他（enter 路径或空 bucket）→ unshift 新 fab_ streaming 槽
    // 同时切 inProgress=true + streamPhase='feeling'；timeout 路径清 pendingPrefill（ai 重跑，prefill 不再需要）
    set((state) => {
      const bucket = state.messagesBySession.get(sid) ?? emptyBucket();
      const head = bucket.messages[0];
      let nextMessages: Message[];
      if (
        head?.role === 'ai' &&
        (head.status === 'failed' || head.status === 'reconnecting') &&
        !head.id.startsWith(FAB_ID_PREFIX)
      ) {
        nextMessages = [
          {
            ...head,
            status: 'streaming',
            content: '',
            stoppedTag: undefined,
            finishReason: undefined,
          },
          ...bucket.messages.slice(1),
        ];
      } else if (head?.id.startsWith(FAB_ID_PREFIX)) {
        nextMessages = [
          { ...head, status: 'streaming', content: '' },
          ...bucket.messages.slice(1),
        ];
      } else {
        const fabSlot: Message = {
          id: genFabricateId(),
          sid,
          role: 'ai',
          content: '',
          status: 'streaming',
          createdAt: new Date().toISOString(),
        };
        nextMessages = [fabSlot, ...bucket.messages];
      }
      const next = new Map(state.messagesBySession);
      next.set(sid, {
        ...bucket,
        messages: nextMessages,
        inProgress: true,
        streamPhase: 'feeling',
      });
      return {
        messagesBySession: next,
        ...(ctx === 'timeout' ? { pendingPrefill: null } : {}),
      };
    });

    let count = 0;
    let handle: ReturnType<typeof setInterval>;

    const tick = async () => {
      // race guard #1: clearInterval 后 RN 仍可能调度本 tick → handle mismatch 退出
      if (resumePollHandles.get(sid)?.handle !== handle) return;
      count++;

      try {
        const branch = await get()._resumeBranchDecide(sid, ctx);
        // race guard #2: await 期间被外部 _stopResumePolling → 退出
        if (resumePollHandles.get(sid)?.handle !== handle) return;

        switch (branch.type) {
          case 'Waiting':
            if (count >= RESUME_POLL_MAX) {
              console.warn(
                '[chatStore] resume polling exhausted, removing fab slot',
                { sid, count },
              );
              toast.show({
                message: '连接异常，请稍后再试',
                variant: 'error',
              });
              get()._stopResumePolling(sid);
              // 超限场景下 fab 槽无对应 orphan human → regenerate chip 链路不可用，
              // 直接移除 fab 槽；用户通过下一次 sendMessage 自然恢复
              set((state) => {
                const bucket = state.messagesBySession.get(sid);
                if (!bucket) return {};
                const h = bucket.messages[0];
                if (!h?.id.startsWith(FAB_ID_PREFIX)) return {};
                const next = new Map(state.messagesBySession);
                next.set(sid, {
                  ...bucket,
                  messages: bucket.messages.slice(1),
                });
                return { messagesBySession: next };
              });
            }
            break;
          case 'OK2':
            console.log('[chatStore] resume polling → OK2, reloading bucket', {
              sid,
              count,
            });
            get()._stopResumePolling(sid);
            // 先移除 fab streaming 槽（避免 loadMessages preserve 逻辑保留它与 server ai 双显示）
            set((state) => {
              const bucket = state.messagesBySession.get(sid);
              if (!bucket) return {};
              const h = bucket.messages[0];
              if (!h?.id.startsWith(FAB_ID_PREFIX)) return {};
              const next = new Map(state.messagesBySession);
              next.set(sid, { ...bucket, messages: bucket.messages.slice(1) });
              return { messagesBySession: next };
            });
            try {
              await get().loadMessages(sid);
            } catch (e) {
              console.warn(
                '[chatStore] resume polling OK2 loadMessages failed',
                e,
              );
            }
            break;
          case 'A4Late':
            console.warn(
              '[chatStore] resume polling → A4Late mid-flight, slot → failed',
              { sid },
            );
            get()._stopResumePolling(sid);
            // 中途切 A4Late：把 streaming 槽切 failed（A4 失败态 + chip）
            set((state) => {
              const bucket = state.messagesBySession.get(sid);
              if (!bucket) return {};
              const h = bucket.messages[0];
              if (h?.role !== 'ai' || h.status !== 'streaming') return {};
              const next = new Map(state.messagesBySession);
              next.set(sid, {
                ...bucket,
                messages: [
                  { ...h, status: 'failed' },
                  ...bucket.messages.slice(1),
                ],
              });
              return { messagesBySession: next };
            });
            break;
          case 'Active':
            console.warn(
              '[chatStore] resume polling → Active mid-flight, yielding',
              { sid },
            );
            get()._stopResumePolling(sid);
            // active stream 接管；移除 fab 槽避免与 sendMessage 的 ai placeholder 冲突
            set((state) => {
              const bucket = state.messagesBySession.get(sid);
              if (!bucket) return {};
              const h = bucket.messages[0];
              if (!h?.id.startsWith(FAB_ID_PREFIX)) return {};
              const next = new Map(state.messagesBySession);
              next.set(sid, { ...bucket, messages: bucket.messages.slice(1) });
              return { messagesBySession: next };
            });
            break;
        }
      } catch (err) {
        // 网络抖动 / 5xx → 不停 polling，等下一 tick 重试；持续故障到 MAX 自然超限
        console.warn(
          '[chatStore] resume polling tick failed (will retry next tick)',
          err,
        );
      }
    };

    handle = setInterval(tick, RESUME_POLL_INTERVAL_MS);
    resumePollHandles.set(sid, { handle });
    return handle as unknown as number;
  },

  _stopResumePolling: (sid) => {
    const existing = resumePollHandles.get(sid);
    if (existing) {
      clearInterval(existing.handle);
      resumePollHandles.delete(sid);
    }
    set((state) => {
      const bucket = state.messagesBySession.get(sid);
      if (!bucket) return {};
      const next = new Map(state.messagesBySession);
      next.set(sid, { ...bucket, inProgress: false, streamPhase: 'idle' });
      return { messagesBySession: next };
    });
  },

  _onSseEvent: (storeKey, event) => {
    // Step 4b · Fast path：delta 帧仅推 buffer，不触发 React setState。
    // streamPhase 切换 'delta' 与 content 累加由组件级 50ms flush tick 调用
    // _appendFlushedDelta 统一回写，避免高频 setState 卡顿。
    if (event.type === 'delta') {
      if (!get().activeStreams.has(storeKey)) return;
      dispatchBufferAppend(storeKey, event.content);
      return;
    }

    // M9.5 · flow_pause 背压帧：复用 backgroundClose Resume 通道（A 方案）。
    // - abort('backgroundClose') → onClose → _cleanupStream('backgroundClose')：
    //   保留 ai 'streaming' 态 + inProgress/streamPhase、删 activeStream，
    //   并把 chatStream 内 closed 置真 → 挡掉随后连接关闭的 transport 'error' 帧（防双处理）。
    // - 立即在前台触发 _handleAppStateActive（与 AppState active 同构）：
    //   弱网 → _resumeBranchDecide 抛 ApiError → catch → _markDisconnected 降级（↻ 重连）；
    //   网络已恢复 / 仅瞬时背压 → OK2 → loadMessages 拉权威态秒渲染整段 ai。
    if (event.type === 'flow_pause') {
      const stream = get().activeStreams.get(storeKey);
      if (!stream) return;
      stream.handle.abort('backgroundClose');
      void get()._handleAppStateActive(storeKey);
      return;
    }

    let migratedTo: SessionId | undefined;

    set((state) => {
      const stream = state.activeStreams.get(storeKey);
      if (!stream) {
        // 流已 cleanup 但事件 race 到达，安全忽略
        return {};
      }

      switch (event.type) {
        case 'session_meta': {
          const newKey = event.session_id;
          const hid = event.hid;

          // 同 sid：仅回填 user msg serverId（streamPhase 保持当前值，等 compression_start 或 thinking_start 切换）
          if (newKey === storeKey) {
            const bucket = state.messagesBySession.get(storeKey);
            if (!bucket) return {};
            const nextMessages = new Map(state.messagesBySession);
            nextMessages.set(storeKey, {
              ...bucket,
              messages: bucket.messages.map((m) =>
                m.id === stream.tempUserId ? { ...m, serverId: hid } : m,
              ),
            });
            return { messagesBySession: nextMessages };
          }

          // sid migrate：把 temp user + AI 占位从旧桶搬到新桶
          const oldBucket = state.messagesBySession.get(storeKey);
          if (!oldBucket) return {};

          const newBucket =
            state.messagesBySession.get(newKey) ?? emptyBucket();

          const migrated: Message[] = [];
          const remaining: Message[] = [];
          for (const m of oldBucket.messages) {
            if (m.id === stream.tempUserId) {
              migrated.push({ ...m, sid: newKey, serverId: hid });
            } else if (m.id === stream.tempAiId) {
              migrated.push({ ...m, sid: newKey });
            } else {
              remaining.push(m);
            }
          }

          const nextMessages = new Map(state.messagesBySession);

          // 旧桶处理：PENDING 临时桶直接删；真实旧 sid 桶保留剩余历史 + 收束态
          if (storeKey === PENDING_SESSION_KEY) {
            nextMessages.delete(storeKey);
          } else {
            nextMessages.set(storeKey, {
              ...oldBucket,
              messages: remaining,
              inProgress: false,
              streamPhase: 'idle',
            });
          }

          // 新桶：prepend migrated（inverted FlatList newest at 0）
          nextMessages.set(newKey, {
            ...newBucket,
            messages: [...migrated, ...newBucket.messages],
            inProgress: true,
            streamPhase: 'feeling',
            lastFetchedAt: Date.now(),
          });

          // activeStreams 搬迁
          const nextStreams = new Map(state.activeStreams);
          nextStreams.delete(storeKey);
          nextStreams.set(newKey, { ...stream, sid: newKey });

          // activeSessionId / todaySessionId 透明切换（若仍指向旧 storeKey，含 PENDING）
          const nextActiveSessionId =
            state.activeSessionId === storeKey ? newKey : state.activeSessionId;
          const nextTodaySessionId =
            state.todaySessionId === storeKey ? newKey : state.todaySessionId;

          migratedTo = newKey;

          return {
            messagesBySession: nextMessages,
            activeStreams: nextStreams,
            activeSessionId: nextActiveSessionId,
            todaySessionId: nextTodaySessionId,
          };
        }

        case 'compression_start': {
          // M7-patch: 上下文压缩开始（M8-patch 9 事件契约）。
          // streamPhase 切 'compressing'，占位文案 B 案由 AIMessage 锁定（4a-patch.3）。
          // payload 当前为空 {}，forward-compat 时再消费字段。
          const bucket = state.messagesBySession.get(storeKey);
          if (!bucket) return {};
          const nextMessages = new Map(state.messagesBySession);
          nextMessages.set(storeKey, {
            ...bucket,
            streamPhase: 'compressing',
          });
          return { messagesBySession: nextMessages };
        }

        case 'compression_end': {
          // M7-patch: 压缩成功结束。streamPhase 切回 'feeling' 作为过渡态，
          // 让用户感知到「压缩完成、AI 在重新启动思考」；后续 thinking_start 接管切 'thinking'。
          // 4 段过渡节奏：feeling → compressing → feeling → thinking。
          // 契约保证 compression_start/end 之间不插 delta，无需清理 content。
          const bucket = state.messagesBySession.get(storeKey);
          if (!bucket) return {};
          const nextMessages = new Map(state.messagesBySession);
          nextMessages.set(storeKey, {
            ...bucket,
            streamPhase: 'feeling',
          });
          return { messagesBySession: nextMessages };
        }

        case 'thinking_start': {
          const bucket = state.messagesBySession.get(storeKey);
          if (!bucket) return {};
          const nextMessages = new Map(state.messagesBySession);
          nextMessages.set(storeKey, {
            ...bucket,
            streamPhase: 'thinking',
          });
          return { messagesBySession: nextMessages };
        }

        case 'thinking_end': {
          const bucket = state.messagesBySession.get(storeKey);
          if (!bucket) return {};
          const nextMessages = new Map(state.messagesBySession);
          nextMessages.set(storeKey, {
            ...bucket,
            streamPhase: 'delta',
          });
          return { messagesBySession: nextMessages };
        }

        // 'delta' 已被 _onSseEvent 顶部 fast path 处理，不再进入此 set 块

        case 'end': {
          // 回填 AI msg serverId + finishReason；status 收束由 _cleanupStream('end') 兜底
          const bucket = state.messagesBySession.get(storeKey);
          if (!bucket) return {};
          const nextMessages = new Map(state.messagesBySession);
          nextMessages.set(storeKey, {
            ...bucket,
            messages: bucket.messages.map((m) =>
              m.id === stream.tempAiId
                ? {
                    ...m,
                    serverId: event.aid,
                    finishReason: event.finish_reason,
                  }
                : m,
            ),
          });
          return { messagesBySession: nextMessages };
        }

        case 'stopped': {
          // Step 5 修订 · 仅记录服务端权威元数据（serverId / finishReason / stoppedTag）；
          // **不改 status**，避免 status='streaming' → 'committed' 切换提早卸载 useStreamBuffer 的 sink，
          // 导致后续 _cleanupStream('stopped') 触发的 dispatchBufferFlushFinal 没有 sink 接收 → 残余 chars 丢失。
          // status 切换延后到 _cleanupStream，由 m.stoppedTag 二分判定（aid 有 → committed，aid 无 → failed）。
          const bucket = state.messagesBySession.get(storeKey);
          if (!bucket) return {};
          const hasAid = event.aid != null && event.aid !== '';
          const nextMessages = new Map(state.messagesBySession);
          nextMessages.set(storeKey, {
            ...bucket,
            messages: bucket.messages.map((m) =>
              m.id === stream.tempAiId
                ? {
                    ...m,
                    serverId: event.aid ?? m.serverId,
                    finishReason: event.finish_reason,
                    stoppedTag: hasAid ? true : m.stoppedTag,
                  }
                : m,
            ),
          });
          return { messagesBySession: nextMessages };
        }

        case 'error': {
          // 终止帧；AI message status='failed' 收束由 _cleanupStream('error') 处理
          //（chatStream.ts onClose 回调统一调用，streamPhase 在 _cleanupStream 内切回 'idle'）。
          // M7-patch: code 枚举 = CompressionError / InternalError（未知按 InternalError 处理）。
          // 按 M7 §5.3：M7 范围内统一进 A4 失败态，不按 code 区分 UI 文案；
          // 失败态视觉 + 重新生成归 Step 6（A4），错误反馈映射归 Step 7（useChatErrorHandler）。
          console.warn('[chatStore] sse error frame', {
            storeKey,
            code: event.code,
            message: event.message,
          });
          return {};
        }

        default:
          return {};
      }
    });

    return migratedTo;
  },

  _cleanupStream: (storeKey, reason, meta) => {
    // Step 7 · meta.transportStatus 存在 → transport 错误 / firstFrameTimeout 派发到 useChatErrorHandler hook。
    // 业务 error 帧（CompressionError / InternalError）路径：chatStream.ts onClose **不带 meta** → meta undefined → 跳过派发，
    // 走下方 buffer + status 收束链路，AI 气泡进 A4 失败态由 Step 6 视觉接管。
    // firstFrameTimeout 路径：当前 chatStream.ts 调 `close('firstFrameTimeout')` 不带 meta，Step 8 接 resumeOnTimeout 时再补 hook 触达点。
    if (
      (reason === 'error' || reason === 'firstFrameTimeout') &&
      meta?.transportStatus != null &&
      onChatErrorHandler
    ) {
      onChatErrorHandler({
        sid: storeKey,
        status: meta.transportStatus,
        source: 'streamTransport',
        reason,
      });
    }

    // Step 4b · 先把组件级 bufferRef 处理掉，再做 status 收束。
    // - error → 丢弃未 flush chunk，保留已 flush 内容（§3.9「保留已渲染部分」）
    // - 其他（end / stopped / abort / firstFrameTimeout）→ 把残余 chunk 推完
    // 顺序关键：stopped/abort 两态判定依赖 m.content 长度。
    if (reason === 'error') {
      dispatchBufferClear(storeKey);
    } else {
      dispatchBufferFlushFinal(storeKey);
    }

    // Step 6 · 在 set 删除 activeStreams[storeKey] 前快照 isRegenerate，
    // 用于 set 之后的 loadMessages 兜底判断（setter 内 state 不可逃逸到 async 路径）。
    const streamSnapshot = get().activeStreams.get(storeKey);

    set((state) => {
      const stream = state.activeStreams.get(storeKey);

      const nextStreams = new Map(state.activeStreams);
      nextStreams.delete(storeKey);

      const nextMessages = new Map(state.messagesBySession);
      const bucket = nextMessages.get(storeKey);

      if (bucket && stream) {
        const updatedMessages = bucket.messages.map((m) => {
          if (m.id !== stream.tempAiId) return m;
          let nextStatus: MessageStatus = 'committed';
          let stoppedTag: boolean | undefined = m.stoppedTag;
          switch (reason) {
            case 'end':
              nextStatus = 'committed';
              break;
            case 'stopped':
              // Step 5 修订 · _onSseEvent('stopped') 仅记录 stoppedTag（aid 有 → true）；
              // 此处按 m.stoppedTag 二分判定 status —— buffer 已在本函数顶部 dispatchBufferFlushFinal 完成，
              // m.content 已反映最终全部内容（含 stopped 帧到达前未 flush 的残余 chars）。
              // - stoppedTag=true（StopWithAi，aid 存在）→ committed
              // - stoppedTag=undefined/false（StopNoAi，aid 缺失）→ 'stopped'（用户语义级中断，非系统失败）
              // - race 兜底（stopped 事件丢失）：保守切 'stopped'，避免被误判为系统失败
              if (m.stoppedTag) {
                nextStatus = 'committed';
              } else {
                nextStatus = 'stopped';
              }
              break;
            case 'error':
              // 后端通过 SSE error 帧通知（CompressionError | InternalError）。
              // 后端错误路径不写 ai 行 → human 仍是孤儿，等下轮新消息走 Row 5 discarded。
              // 前端同步对齐：清空 partial content，让 gate `(failed||stopped) && content=0`
              // 在下次 sendMessage / regenerate 时命中，顶替孤儿 ai 头槽 → 与后端最终态一致。
              m.content = '';
              nextStatus = 'failed';
              break;
            case 'firstFrameTimeout':
              // first frame 之前 content 必然 = 0，无需清。
              nextStatus = 'failed';
              break;
            case 'abort':
              nextStatus = m.content.length > 0 ? 'committed' : 'stopped';
              break;
            case 'backgroundClose':
              // Step 9 · AppState background 触发的主动关闭。
              // 不收束 status：维持 'streaming'，后续 _startReconnecting 切 'reconnecting' 接管视觉。
              // bucket.inProgress / streamPhase 也保留（下方 set 收尾会覆盖，需要分支保护 — 见 set 出口）。
              // buffer 已在本函数顶部 dispatchBufferFlushFinal 推完。
              return m;
          }
          return { ...m, status: nextStatus, stoppedTag };
        });
        nextMessages.set(storeKey, {
          ...bucket,
          messages: updatedMessages,
          // backgroundClose 维持 inProgress/streamPhase，等 _startReconnecting 接管；
          // 其他 reason 收尾 idle。
          ...(reason === 'backgroundClose'
            ? {}
            : { inProgress: false, streamPhase: 'idle' }),
        });
      } else if (bucket) {
        nextMessages.set(storeKey, {
          ...bucket,
          // Step 9 · backgroundClose 维持 inProgress/streamPhase，等 _startReconnecting 接管；
          // 其他 reason 收尾 idle。与上方 if 分支处理一致。
          ...(reason === 'backgroundClose'
            ? {}
            : { inProgress: false, streamPhase: 'idle' }),
        });
      }

      // Step 7 · A4Late prefill：firstFrameTimeout 时把用户刚发的 user msg 内容回灌到 ChatInput，
      // 便于用户原文修改后再发；其他 reason 不触发（end/stopped/abort/error 都不是「未达后端」场景）。
      // user msg 在 sendMessage 乐观插入时就入 bucket，_cleanupStream 不删它，所以 find 必中。
      // PENDING 路径（session_meta 未到 → storeKey === PENDING_SESSION_KEY）user msg 仍在 PENDING bucket 内，逻辑一致。
      // Step 9 · 扩展 prefill 触发条件：
      // - firstFrameTimeout：原逻辑保持（首帧超时必然 user.serverId 缺失）
      // - error：补 5xx + session_meta 未达 的 gap（user.serverId 缺失 = 后端 0 记录 = 必须 prefill 回灌）
      // - 统一判定逻辑：(firstFrameTimeout || error || abort) && user.serverId 缺失
      // 当日首次对话时发消息，发出后立即停止，无sid，无hid。前端走regenerate不通，会走 abort 路径。
      const userMsg =
        stream && bucket
          ? bucket.messages.find((m) => m.id === stream.tempUserId)
          : undefined;
      const prefillCandidate =
        (reason === 'firstFrameTimeout' ||
          reason === 'error' ||
          reason === 'abort') &&
        userMsg != null &&
        !userMsg.serverId
          ? userMsg.content
          : undefined;

      return {
        activeStreams: nextStreams,
        messagesBySession: nextMessages,
        ...(prefillCandidate ? { pendingPrefill: prefillCandidate } : {}),
      };
    });

    // Step 6 · 重新生成失败兜底：error / firstFrameTimeout 时静默调 loadMessages，
    // 让 bucket 与服务端一致（Step 8 决策器接管细分；本 Step 不区分 HTTP 400/5xx）。
    // stopped / abort / end 路径不触发 — _cleanupStream 已写好正确终态。
    if (
      streamSnapshot?.isRegenerate &&
      (reason === 'error' || reason === 'firstFrameTimeout')
    ) {
      void get()
        .loadMessages(storeKey)
        .catch((e) => {
          console.warn(
            '[chatStore] regenerate failure: loadMessages fallback failed',
            e,
          );
        });
    }

    // Step 8 · firstFrameTimeout 自纠错：触发 resumeOnTimeout 决策器异步纠正
    //（5s 内首帧未到，但后端可能在 5-200s 区间内真返回）。
    // Gate：
    // - reason === 'firstFrameTimeout'：仅首帧超时；transport error / business error / end / stopped / abort 不触发
    // - storeKey !== PENDING_SESSION_KEY：PENDING 无真 sid 可查，pendingPrefill 链路已接管
    // - !streamSnapshot?.isRegenerate：regenerate 失败上方已有 loadMessages 兜底，叠加决策无意义
    if (
      reason === 'firstFrameTimeout' &&
      storeKey !== PENDING_SESSION_KEY &&
      !streamSnapshot?.isRegenerate
    ) {
      void get()
        .resumeOnTimeout(storeKey)
        .catch((e) => {
          console.warn(
            '[chatStore] firstFrameTimeout: resumeOnTimeout failed',
            e,
          );
        });
    }

    console.log('[chatStore] _cleanupStream', { storeKey, reason });
  },

  _handleAppStateActive: async (sid) => {
    // Step 9 · AppState background→active 入口。
    // 调用前置条件（由 app/_layout.tsx 接线时保证）：
    // - background 时 activeStreams.size > 0（gate flag）
    // - sid = todaySessionId
    // 流程：reconnecting 视觉 → 决策器 → 按分支收口 / 失败切 disconnected。
    get()._startReconnecting(sid);
    try {
      const branch = await get()._resumeBranchDecide(sid, 'enter');
      await get()._completeReconnecting(sid, branch);
    } catch (e) {
      // ApiError：fetch 60s 超时 / 网络层 reject / 5xx 等。
      // 切 disconnected 等用户主动重连。底层不再循环重试（与 60s 单计时器简化方案一致）。
      console.warn(
        '[chatStore] _handleAppStateActive: decide failed → disconnected',
        e,
      );
      get()._markDisconnected(sid);
    }
  },

  _retryReconnect: async (sid) => {
    // Step 9 · 用户点「重新连接」chip 入口。
    // disconnected → reconnecting，重新跑决策器一轮。
    set((state) => {
      const bucket = state.messagesBySession.get(sid);
      if (!bucket) return {};
      const head = bucket.messages[0];
      if (head?.role !== 'ai' || head.status !== 'disconnected') return {};
      const next = new Map(state.messagesBySession);
      next.set(sid, {
        ...bucket,
        messages: [
          { ...head, status: 'reconnecting' },
          ...bucket.messages.slice(1),
        ],
      });
      return { messagesBySession: next };
    });
    try {
      const branch = await get()._resumeBranchDecide(sid, 'enter');
      await get()._completeReconnecting(sid, branch);
    } catch (e) {
      console.warn(
        '[chatStore] _retryReconnect: decide failed → disconnected',
        e,
      );
      get()._markDisconnected(sid);
    }
  },

  _startReconnecting: (sid) => {
    // Step 9 · bucket[0] streaming AI 残留槽切 reconnecting。
    // 仅在 backgroundClose 后留下的 streaming AI 槽上生效；
    // bucket 空 / head 非 streaming AI → 静默 no-op（保护语义）。
    set((state) => {
      const bucket = state.messagesBySession.get(sid);
      if (!bucket) return {};
      const head = bucket.messages[0];
      if (head?.role !== 'ai' || head.status !== 'streaming') return {};
      const next = new Map(state.messagesBySession);
      next.set(sid, {
        ...bucket,
        messages: [
          { ...head, status: 'reconnecting' },
          ...bucket.messages.slice(1),
        ],
        // inProgress 保持 true（仍在「与服务端对齐」中），streamPhase 切 idle（不展示 thinking/delta 占位文案）
        streamPhase: 'idle',
      });
      return { messagesBySession: next };
    });
  },

  _markDisconnected: (sid) => {
    // Step 9 · reconnecting → disconnected。
    // 视觉切「与服务器断开连接 + 重新连接 chip」；inProgress 仍 true（仍未明确终态）。
    set((state) => {
      const bucket = state.messagesBySession.get(sid);
      if (!bucket) return {};
      const head = bucket.messages[0];
      if (head?.role !== 'ai' || head.status !== 'reconnecting') return {};
      const next = new Map(state.messagesBySession);
      next.set(sid, {
        ...bucket,
        messages: [
          { ...head, status: 'disconnected' },
          ...bucket.messages.slice(1),
        ],
      });
      return { messagesBySession: next };
    });
  },

  _completeReconnecting: async (sid, branch) => {
    // Step 9 · 决策器返回后按分支收口。
    switch (branch.type) {
      case 'OK2': {
        // 删头部 reconnecting 槽，loadMessages 拉权威态覆盖。
        set((state) => {
          const bucket = state.messagesBySession.get(sid);
          if (!bucket) return {};
          const head = bucket.messages[0];
          if (head?.role !== 'ai' || head.status !== 'reconnecting') return {};
          const next = new Map(state.messagesBySession);
          next.set(sid, {
            ...bucket,
            messages: bucket.messages.slice(1),
          });
          return { messagesBySession: next };
        });
        try {
          await get().loadMessages(sid);
        } catch (e) {
          console.warn(
            '[chatStore] _completeReconnecting OK2 loadMessages failed',
            e,
          );
        }
        break;
      }
      case 'Waiting': {
        // _startResumePolling 内 reconnecting 复用分支命中（改动 2.3）：
        // 切 streaming + 启 setInterval 每 2s 决策。
        get()._startResumePolling(sid, 'enter');
        break;
      }
      case 'A4Late': {
        // reconnecting → failed（A4 失败态 + 重新生成 chip）。
        // bucket[1] 是真 human + serverId 通常已有（session_meta 在 backgroundClose 前到达）→ chip 可见。
        // Step 9 方案 A · corner case 兜底：极快切后台（session_meta 未达就 backgroundClose）→
        // bucket[1].serverId 缺失,regenerate 会 invariant violated → 引导用户「直接重发」时
        // ChatInput 应有内容回灌。此时把 bucket[1].content 写到 pendingPrefill,
        // 与 _cleanupStream 内 firstFrameTimeout / error gap 修复对齐。
        set((state) => {
          const bucket = state.messagesBySession.get(sid);
          if (!bucket) return {};
          const head = bucket.messages[0];
          if (head?.role !== 'ai' || head.status !== 'reconnecting') return {};
          const humanRow = bucket.messages[1];
          const prefillCandidate =
            humanRow?.role === 'human' && !humanRow.serverId
              ? humanRow.content
              : undefined;
          const next = new Map(state.messagesBySession);
          next.set(sid, {
            ...bucket,
            messages: [
              { ...head, status: 'failed' },
              ...bucket.messages.slice(1),
            ],
            inProgress: false,
            streamPhase: 'idle',
          });
          return {
            messagesBySession: next,
            ...(prefillCandidate ? { pendingPrefill: prefillCandidate } : {}),
          };
        });
        break;
      }
      case 'Active': {
        // 极小概率 race（decide 期间用户在另一处启动了新 stream）。
        // 删 reconnecting 槽让 active 流接管，bucket 不再保留过时占位。
        set((state) => {
          const bucket = state.messagesBySession.get(sid);
          if (!bucket) return {};
          const head = bucket.messages[0];
          if (head?.role !== 'ai' || head.status !== 'reconnecting') return {};
          const next = new Map(state.messagesBySession);
          next.set(sid, {
            ...bucket,
            messages: bucket.messages.slice(1),
          });
          return { messagesBySession: next };
        });
        break;
      }
    }
  },

  _appendFlushedDelta: (storeKey, aiId, chunk) => {
    if (!chunk) return;
    set((state) => {
      const bucket = state.messagesBySession.get(storeKey);
      if (!bucket) return {};
      const nextMessages = new Map(state.messagesBySession);
      nextMessages.set(storeKey, {
        ...bucket,
        // 首次 flush 时把 phase 推到 'delta'；幂等覆盖无副作用
        streamPhase: 'delta',
        messages: bucket.messages.map((m) =>
          m.id === aiId ? { ...m, content: m.content + chunk } : m,
        ),
      });
      return { messagesBySession: nextMessages };
    });
  },
}));
