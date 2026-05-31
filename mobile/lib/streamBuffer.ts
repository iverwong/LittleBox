/**
 * M7 · Step 4b · 流式 token 调度总线。
 *
 * 设计：
 * - store 收到 SSE `delta` 事件 → 调用 `dispatchBufferAppend(sid, chunk)`，
 *   将 chunk 推到组件级 bufferRef（不触发 React setState）。
 * - 对应 AIMessage 组件通过 `useStreamBuffer` 注册一个 sink，
 *   bufferRef 在 hook 内以 50ms 节奏 flush 到 store.message.content。
 * - 流结束（end / abort / firstFrameTimeout）→ `dispatchBufferFlushFinal(sid)`
 *   把残余 chunk 推进 store；error 帧 → `dispatchBufferClear(sid)` 丢弃未 flush
 *   的部分，已 flush 到 store 的内容保留（与 M7 §3.9 "保留已渲染部分" 一致）。
 *
 * 关键纪律：
 * - buffer 仅组件级 ref；本模块只做 sid → sink 的轻量 dispatcher，无任何状态。
 * - 一个 sid 同一时刻只有一个 streaming AI 气泡，sink 按 sid 单例覆盖；
 *   重复 register 视为接管，旧 sink 自动失效。
 * - 所有 dispatch* 调用对未注册 sid 都是 no-op（流式期间组件未挂载属正常）。
 */

export type BufferSink = {
  append: (chunk: string) => void;
  flushFinal: () => void;
  clear: () => void;
};

const sinks = new Map<string, BufferSink>();

export function registerBufferSink(sid: string, sink: BufferSink): void {
  sinks.set(sid, sink);
}

export function unregisterBufferSink(sid: string, sink: BufferSink): void {
  // 仅当当前 sink 仍是注册中的那一个才清除，避免接管场景误删
  if (sinks.get(sid) === sink) sinks.delete(sid);
}

export function dispatchBufferAppend(sid: string, chunk: string): void {
  if (!chunk) return;
  sinks.get(sid)?.append(chunk);
}

export function dispatchBufferFlushFinal(sid: string): void {
  sinks.get(sid)?.flushFinal();
}

export function dispatchBufferClear(sid: string): void {
  sinks.get(sid)?.clear();
}
