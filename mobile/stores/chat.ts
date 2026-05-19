/**
 * M7 · 聊天 store。
 *
 * Step 1.3 骨架范围：
 *   - loadSessions：消费 GET /me/sessions 顶层 today_session_id + sessions[]
 *   - setActiveSession：切换 activeSessionId
 *   - _cleanupStream：从 activeStreams 删 + inProgress=false + streamPhase='idle'
 *   - _onSseEvent：空骨架仅 console.log（Step 4a 接入完整事件处理）
 *
 * 其余 actions（loadMessages/sendMessage/regenerate/stopStream/resumeOnEnter/
 * resumeOnTimeout/deleteSession/loadMoreMessages）留 stub，保 typecheck 通过，
 * 由 Step 2-8 增量实现。
 *
 * 关键纪律（不可在 1.3 偏离）：
 *   - Map 必须不可变更新（new Map(prev).set(...)）以触发 React re-render
 *   - inProgress 仅 UI hint；权威态以 GET messages 顶层 in_progress + 末行 role 为准
 *   - token buffer 不进 store（组件级 ref，详见 Step 4b useStreamBuffer）
 */

import { create } from 'zustand';
import {
  getMessages,
  listSessions,
  type MessageListItem,
  type SessionId,
} from '@/services/api/chat';
import type {
  SseEvent,
  ChatStreamHandle,
  ChatStreamCloseReason,
} from '@/lib/chatStream';

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
  id: MessageId;
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
  title: string | null; // backend schema: str | None，标题为空时由 UI 兜底显示
  lastActiveAt: string; // ISO datetime
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
};

export type ResumeBranch =
  | { type: 'Active' }
  | { type: 'OK2' }
  | { type: 'Waiting'; pollHandle: number }
  | { type: 'A4Late' };

export type ChatStore = {
  sessions: SessionMeta[]; // 仅历史，永不含今日（后端 sessions[] 已过滤）
  sessionsCursor: string | null;
  sessionsHasMore: boolean;

  todaySessionId: SessionId | null; // 来自 GET /me/sessions 顶层
  activeSessionId: SessionId | null;

  messagesBySession: Map<SessionId, SessionMessageState>;
  activeStreams: Map<SessionId, ActiveStream>;

  // Step 1.3 实现
  loadSessions: (opts?: { reset?: boolean }) => Promise<void>;
  setActiveSession: (sid: SessionId | null) => void;

  // 后续 Step 增量实现，1.3 留 stub
  loadMessages: (sid: SessionId) => Promise<void>;
  loadMoreMessages: (sid: SessionId) => Promise<void>;
  sendMessage: (sid: SessionId | null, content: string) => Promise<void>;
  stopStream: (sid: SessionId) => Promise<void>;
  regenerate: (sid: SessionId, lastHumanMid: MessageId) => Promise<void>;
  resumeOnEnter: (sid: SessionId) => Promise<ResumeBranch>;
  resumeOnTimeout: (sid: SessionId) => Promise<ResumeBranch>;
  deleteSession: (sid: SessionId) => Promise<void>;

  // SSE 内部
  _onSseEvent: (sid: SessionId, event: SseEvent) => void;
  _cleanupStream: (sid: SessionId, reason: ChatStreamCloseReason) => void;
};

function mapApiMessageToStore(item: MessageListItem, sid: SessionId): Message {
  // 后端 messages.items 已过滤 discarded，全部按已固化态映射；
  // finishReason 透传，便于 Step 5 stoppedTag 判定与 Step 6 A4 失败态识别。
  return {
    id: item.id,
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
      // 401/5xx 由 API client 层全局兜底（M5 sinks）；4xx 业务码在 Step 7 映射 UI 反馈
      // 1.3 阶段先 throw，便于调用方（Step 1.4 chat/index.tsx）感知失败
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
        // 后端按 created_at desc 返回；前端配合 inverted FlatList，
        // 数组首位 = newest，无需 reverse。
        messages: data.items.map((item) => mapApiMessageToStore(item, sid)),
        hasMore: data.next_cursor != null,
        cursor: data.next_cursor,
        lastFetchedAt: Date.now(),
        // in_progress 顶层来自 Redis chat:lock:{sid}（权威态）；
        // Step 2 仅写入，不接 Resume（Step 8 补 resumeOnEnter）。
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
        // inverted FlatList 模式，更老一批 append 到数组末尾。
        messages: [
          ...prev.messages,
          ...data.items.map((item) => mapApiMessageToStore(item, sid)),
        ],
        hasMore: data.next_cursor != null,
        cursor: data.next_cursor,
        // lastFetchedAt 故意不更新：避免上滚加载更多变相延长 30s 缓存窗口。
      });
      return { messagesBySession: nextMessages };
    });
  },
  sendMessage: (sid, content) => {
    void sid;
    void content;
    return notImplemented('sendMessage');
  },
  stopStream: (sid) => {
    void sid;
    return notImplemented('stopStream');
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

  _onSseEvent: (sid, event) => {
    // 1.3 骨架：仅 console.log；Step 4a 接入完整事件处理
    console.log('[chatStore] _onSseEvent', { sid, event });
  },

  _cleanupStream: (sid, reason) => {
    set((state) => {
      const nextStreams = new Map(state.activeStreams);
      nextStreams.delete(sid);

      const nextMessages = new Map(state.messagesBySession);
      const prevSessionState = nextMessages.get(sid);
      if (prevSessionState) {
        nextMessages.set(sid, {
          ...prevSessionState,
          inProgress: false,
          streamPhase: 'idle',
        });
      }

      return {
        activeStreams: nextStreams,
        messagesBySession: nextMessages,
      };
    });

    console.log('[chatStore] _cleanupStream', { sid, reason });
  },
}));
