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
	let closedByUs = false
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
		// 用 closedByUs 标志区分「我们主动关」vs「底层出错」，避免 UI 误跳 error 态。
		if (closedByUs) return
		handlers.onTransportError?.(err)
	})

	return {
		close: () => {
			closedByUs = true
			es.close()
		},
	}
}
