// TODO(M7 cleanup): delete this file; frontend counterpart of dev_chat.py (M3 demo page)
import { useRef, useState } from 'react'
import { Button, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native'
import { router } from 'expo-router'

import { ChatSseHandle, openChatStream } from '../../lib/sseClient'

type Status = 'idle' | 'streaming' | 'done' | 'error'

// 开发地址：Android 模拟器用 10.0.2.2，iOS 模拟器 / web 用 localhost，真机用电脑局域网 IP。
// 生产走 EXPO_PUBLIC_API_BASE 环境变量；M7 清理时此常量一并删除。
// 网络环境变化时，host ip需要修改后方可进行测试
const API_BASE = 'http://192.168.1.3:50060'

export default function DevChat() {
	const [input, setInput] = useState('')
	const [reply, setReply] = useState('')
	const [status, setStatus] = useState<Status>('idle')
	const [errMsg, setErrMsg] = useState<string | null>(null)
	const handleRef = useRef<ChatSseHandle | null>(null)

	const send = () => {
		if (!input.trim() || status === 'streaming') return
		setReply('')
		setErrMsg(null)
		setStatus('streaming')

		handleRef.current = openChatStream(API_BASE, input, {
			onEvent: (e) => {
				switch (e.type) {
					case 'delta':
						setReply((prev) => prev + e.content)
						break
					case 'end':
						setStatus('done')
						break
					case 'error':
						setErrMsg(e.message)
						setStatus('error')
						break
				}
			},
			onTransportError: (err) => {
				const msg = (err as { message?: string }).message ?? (err instanceof Error ? err.message : String(err))
				setErrMsg(msg)
				setStatus('error')
			},
		})
	}

	const stop = () => {
		handleRef.current?.close()
		setStatus('done')
	}

	return (
		<View style={styles.container}>
			<Text style={styles.title}>M3 Streaming Demo（M7 后删除）</Text>
			{/* [M15-TEMP] 临时入口，展厅验收完成后删除 */}
			<Button title="开发展厅" onPress={() => router.push('/dev/components' as never)} />
			<TextInput
				style={styles.input}
				value={input}
				onChangeText={setInput}
				placeholder="输入一条消息"
				editable={status !== 'streaming'}
			/>
			<View style={styles.row}>
				<Button title="发送" onPress={send} disabled={status === 'streaming'} />
				<Button title="停止" onPress={stop} disabled={status !== 'streaming'} />
			</View>
			<Text style={styles.status}>状态：{status}</Text>
			<ScrollView style={styles.replyBox}>
				<Text style={styles.replyText}>{reply}</Text>
				{errMsg && <Text style={styles.err}>错误：{errMsg}</Text>}
			</ScrollView>
		</View>
	)
}

const styles = StyleSheet.create({
	container: { flex: 1, padding: 16, gap: 12, backgroundColor: '#ffffff' },
	title: { fontSize: 18, fontWeight: '700', color: '#111111', marginBottom: 4 },
	input: { borderWidth: 1, borderColor: '#333333', borderRadius: 8, padding: 12, fontSize: 16, color: '#111111', backgroundColor: '#ffffff' },
	row: { flexDirection: 'row', gap: 12 },
	status: { color: '#333333', fontSize: 14 },
	replyBox: { flex: 1, borderWidth: 1, borderColor: '#cccccc', borderRadius: 8, padding: 12, backgroundColor: '#fafafa' },
	replyText: { fontSize: 16, color: '#111111', lineHeight: 24 },
	err: { color: '#cc0000', marginTop: 8, fontSize: 14 },
})
