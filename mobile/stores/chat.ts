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
} from '@/lib/chatStream';
import {
  dispatchBufferAppend,
  dispatchBufferClear,
  dispatchBufferFlushFinal,
} from '@/lib/streamBuffer';

export type MessageId = string;

export type MessageRole = 'human' | 'ai';

export type MessageStatus = 'committed' | 'streaming' | 'failed' | 'stopped';

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

  messagesBySession: Map<SessionId, SessionMessageState>;
  activeStreams: Map<SessionId, ActiveStream>;

  loadSessions: (opts?: { reset?: boolean }) => Promise<void>;
  setActiveSession: (sid: SessionId | null) => void;

  loadMessages: (sid: SessionId) => Promise<void>;
  loadMoreMessages: (sid: SessionId) => Promise<void>;
  sendMessage: (sid: SessionId | null, content: string) => Promise<void>;
  stopStream: (sid: SessionId) => Promise<void>;
  regenerate: (sid: SessionId, lastHumanMid: MessageId) => Promise<void>;
  resumeOnEnter: (sid: SessionId) => Promise<ResumeBranch>;
  resumeOnTimeout: (sid: SessionId) => Promise<ResumeBranch>;
  deleteSession: (sid: SessionId) => Promise<void>;

  /**
   * SSE event handler.
   * 返回值：session_meta migrate 时返回新 storeKey；其他事件返回 void。
   * 由 sendMessage 闭包通过 ctx.storeKey 持有最新 key，避免 onEvent 闭包失效。
   */
  _onSseEvent: (storeKey: SessionId, event: SseEvent) => SessionId | void;
  _cleanupStream: (storeKey: SessionId, reason: ChatStreamCloseReason) => void;
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

// sid=null 路径的临时桶 key；session_meta 到来时必 migrate 到真实 sid
export const PENDING_SESSION_KEY: SessionId = '__pending__';

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
  messagesBySession: new Map(),
  activeStreams: new Map(),

  loadSessions: async (opts) => {
    const reset = opts?.reset ?? false;
    const cursor = reset ? undefined : (get().sessionsCursor ?? undefined);

    const result = await listSessions({ cursor, limit: 15 });

    if (!result.ok) {
      throw new Error(`[chatStore] loadSessions failed: HTTP ${result.status}`);
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

  loadMessages: async (sid) => {
    const result = await getMessages(sid, { limit: 50 });
    if (!result.ok) {
      throw new Error(`[chatStore] loadMessages failed: HTTP ${result.status}`);
    }
    const data = result.data;

    set((state) => {
      const nextMessages = new Map(state.messagesBySession);
      nextMessages.set(sid, {
        messages: data.items.map((item) => mapApiMessageToStore(item, sid)),
        hasMore: data.next_cursor != null,
        cursor: data.next_cursor,
        lastFetchedAt: Date.now(),
        inProgress: data.in_progress,
        streamPhase: 'idle',
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
      throw new Error(
        `[chatStore] loadMoreMessages failed: HTTP ${result.status}`,
      );
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
      return { messagesBySession: next };
    });

    // 闭包内可变 ctx，承载 session_meta migrate 后的新 storeKey
    const ctx: { storeKey: SessionId } = { storeKey: initialKey };

    const handle = openChatStream({
      sid,
      content: trimmed,
      onEvent: (event) => {
        const newKey = get()._onSseEvent(ctx.storeKey, event);
        if (newKey) ctx.storeKey = newKey;
      },
      onClose: (reason) => {
        get()._cleanupStream(ctx.storeKey, reason);
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

    const result = await stopSession(sid);
    if (!result.ok) {
      // Step 7 错误码映射会接管 toast；本 Step 仅 log + abort 兜底
      console.warn('[chatStore] stopStream: stop API failed, aborting handle', {
        sid,
        status: result.status,
      });
      stream.handle.abort();
    }
    // 成功路径不在此处做 cleanup；服务端 stopped 帧到达后由 onEvent + onClose 串行触发
  },
  regenerate: (sid, lastHumanMid) => {
    void sid;
    void lastHumanMid;
    return notImplemented('regenerate');
  },
  resumeOnEnter: (sid) => {
    void sid;
    return notImplemented('resumeOnEnter');
  },
  resumeOnTimeout: (sid) => {
    void sid;
    return notImplemented('resumeOnTimeout');
  },
  deleteSession: (sid) => {
    void sid;
    return notImplemented('deleteSession');
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

          // activeSessionId 透明切换（若用户当前看的就是旧 storeKey）
          const nextActiveSessionId =
            state.activeSessionId === storeKey ? newKey : state.activeSessionId;

          migratedTo = newKey;

          return {
            messagesBySession: nextMessages,
            activeStreams: nextStreams,
            activeSessionId: nextActiveSessionId,
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
          // Step 5 · 精确按服务端 event.aid 判别两态（不再依赖 content.length 兜底）：
          // - aid 存在 → StopWithAi（has_emitted_content=true，ai 行已落库）→ committed + stoppedTag
          // - aid 缺失 → StopNoAi（has_emitted_content=false，ai 行未落库）→ failed（进 A4，Step 6 接 UI）
          // 这里直接写 status/stoppedTag，_cleanupStream('stopped') 不再覆写（见下方 cleanup 改动）。
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
                    status: hasAid ? 'committed' : 'failed',
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

  _cleanupStream: (storeKey, reason) => {
    // Step 4b · 先把组件级 bufferRef 处理掉，再做 status 收束。
    // - error → 丢弃未 flush chunk，保留已 flush 内容（§3.9「保留已渲染部分」）
    // - 其他（end / stopped / abort / firstFrameTimeout）→ 把残余 chunk 推完
    // 顺序关键：stopped/abort 两态判定依赖 m.content 长度。
    if (reason === 'error') {
      dispatchBufferClear(storeKey);
    } else {
      dispatchBufferFlushFinal(storeKey);
    }

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
              // Step 5 · _onSseEvent('stopped') 已按服务端 event.aid 精确写入 status/stoppedTag；
              // cleanup 仅在 race 兜底（理论不可能：onClose('stopped') 总在 onEvent('stopped') 后触发）：
              // 若 message 仍为 'streaming'，说明 stopped 事件未到 onEvent 即 close，按 content 长度兜底。
              if (m.status === 'streaming') {
                if (m.content.length > 0) {
                  nextStatus = 'committed';
                  stoppedTag = true;
                } else {
                  nextStatus = 'failed';
                }
              } else {
                nextStatus = m.status;
                // stoppedTag 已由 let 初始化从 m.stoppedTag 取，无需再赋值
              }
              break;
            case 'error':
            case 'firstFrameTimeout':
              nextStatus = 'failed';
              break;
            case 'abort':
              // 用户语义级中断：内容保留固化，无 stoppedTag
              nextStatus = m.content.length > 0 ? 'committed' : 'failed';
              break;
          }
          return { ...m, status: nextStatus, stoppedTag };
        });
        nextMessages.set(storeKey, {
          ...bucket,
          messages: updatedMessages,
          inProgress: false,
          streamPhase: 'idle',
        });
      } else if (bucket) {
        nextMessages.set(storeKey, {
          ...bucket,
          inProgress: false,
          streamPhase: 'idle',
        });
      }

      return {
        activeStreams: nextStreams,
        messagesBySession: nextMessages,
      };
    });

    console.log('[chatStore] _cleanupStream', { storeKey, reason });
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
