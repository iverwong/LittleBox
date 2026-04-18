import EventSource from 'react-native-sse'

export type ChatSseEvent =
	| { type: 'start'; session_id: string }
	| { type: 'delta'; content: string }
	| { type: 'end'; finish_reason: string }
	| { type: 'error'; message: string; code: string }

export interface ChatSseHandle {
	close: () => void
}

export function openChatStream(
	baseUrl: string,
	message: string,
	handlers: {
		onEvent: (e: ChatSseEvent) => void
		onTransportError?: (err: unknown) => void
	},
): ChatSseHandle {
	// react-native-sse 支持 POST + body，原生 EventSource 不支持，这是选它的关键原因。
	const es = new EventSource(`${baseUrl}/api/dev/chat/stream`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ message }),
	})

	es.addEventListener('message', (ev) => {
		if (!ev.data) return
		try {
			const parsed = JSON.parse(ev.data) as ChatSseEvent
			handlers.onEvent(parsed)
			if (parsed.type === 'end' || parsed.type === 'error') es.close()
		} catch (err) {
			handlers.onTransportError?.(err)
		}
	})
	es.addEventListener('error', (err) => {
		// react-native-sse 在 close() 后仍会触发一次 error（CONNECTING → CLOSED 转换），
		// 过滤掉已关闭状态的误报，避免 UI 误跳 error 态。
		if ((es as unknown as { readyState: number }).readyState === 2 /* CLOSED */) return
		handlers.onTransportError?.(err)
	})

	return { close: () => es.close() }
}
