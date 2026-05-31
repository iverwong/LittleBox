/**
 * M7 · Step 4b · 组件级 token buffer hook。
 *
 * 行为：
 * - 仅当 `enabled=true`（AI 气泡 status='streaming'）时启用：
 *   1. 注册 sink 接管该 sid 的 delta 分发
 *   2. 启动 50ms setInterval flush tick
 * - flush tick：bufferRef 非空 → 拷出 chunk → 清空 bufferRef → 调 `onFlush(chunk)`
 *   （由调用方写入 store.message.content）
 * - flushFinal：被 store 在 cleanup(end/stopped/abort/firstFrameTimeout) 时调用，
 *   把残余 chunk 推完一次
 * - clear：被 store 在 error 帧 cleanup 时调用，丢弃未 flush 的 bufferRef
 *   （已 flush 到 store 的内容由 store 自己保留，与 §3.9 失败态契约一致）
 *
 * 渲染策略：
 * - 不向外暴露 bufferedText 状态；视觉刷新依赖 onFlush 写 store → store 订阅触发
 *   AIMessage re-render。50ms 节拍 = 20fps，肉眼顺畅，且避免高频 setState 卡顿。
 */
import { useEffect, useRef } from 'react';

import {
  registerBufferSink,
  unregisterBufferSink,
  type BufferSink,
} from '@/lib/streamBuffer';

type Args = {
  sid: string;
  enabled: boolean;
  onFlush: (chunk: string) => void;
  onClear?: () => void;
  intervalMs?: number;
};

export function useStreamBuffer({
  sid,
  enabled,
  onFlush,
  onClear,
  intervalMs = 50,
}: Args): void {
  const bufferRef = useRef<string>('');
  const onFlushRef = useRef(onFlush);
  const onClearRef = useRef(onClear);
  onFlushRef.current = onFlush;
  onClearRef.current = onClear;

  useEffect(() => {
    if (!enabled) return;
    bufferRef.current = '';

    const flushIfNonEmpty = () => {
      if (bufferRef.current.length === 0) return;
      const chunk = bufferRef.current;
      bufferRef.current = '';
      onFlushRef.current(chunk);
    };

    const sink: BufferSink = {
      append: (chunk) => {
        bufferRef.current += chunk;
      },
      flushFinal: flushIfNonEmpty,
      clear: () => {
        bufferRef.current = '';
        onClearRef.current?.();
      },
    };

    registerBufferSink(sid, sink);
    const timerId = setInterval(flushIfNonEmpty, intervalMs);

    return () => {
      clearInterval(timerId);
      unregisterBufferSink(sid, sink);
      bufferRef.current = '';
    };
  }, [sid, enabled, intervalMs]);
}
