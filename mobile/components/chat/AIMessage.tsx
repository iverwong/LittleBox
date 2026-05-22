/**
 * M7 · AI 消息气泡。
 *
 * Step 2 静态版：仅渲染已固化 content
 * Step 4a.3：流式「首 token 未到」态消费 bucket.streamPhase 渲染占位文案 + 动态省略号
 * Step 4b：叠加 useStreamBuffer，AI 流式态采用「已固化 content + buffer chunk」双源
 * Step 5：根据 stoppedTag 渲染「已停止」角标
 * Step 6：根据 status='failed' 渲染 A4 失败占位
 *
 * 设计约束：
 * - streamPhase 是 bucket(SessionMessageState)级状态，不是 Message 字段
 * - 仅当 AI 气泡 status='streaming' && content='' 时消费 phase；首 delta 到达即覆盖占位
 * - 'feeling' phase 当前 SSE 协议未发，文案映射保留为 forward-compat（Step 6 mascot 接入时启用）
 */
import { useEffect, useState } from 'react'
import { StyleSheet, Text, View } from 'react-native'

import type { Message, StreamPhase } from '@/stores/chat'
import { useChatStore } from '@/stores/chat'

type Props = {
    message: Message
}

function placeholderForPhase(
    phase: StreamPhase,
    compressionMessage: string | undefined,
): string | null {
    switch (phase) {
        case 'feeling':
            return '感受中'
        case 'compressing':
            // 后端 compression_progress 帧带的 message（中文文案），无则前端 fallback
            return compressionMessage ?? '正在为对话腾出更多空间'
        case 'thinking':
            return '思考中'
        default:
            // 'idle' / 'delta' / 'interrupted' 不显示文案占位；Step 4b 叠加跳动光标
            return null
    }
}

/** 动态省略号：. → .. → … → . 循环，500ms 一帧 */
function useEllipsisDots(intervalMs = 500): string {
    const [count, setCount] = useState(1)
    useEffect(() => {
        const id = setInterval(() => {
            setCount((c) => (c % 3) + 1)
        }, intervalMs)
        return () => clearInterval(id)
    }, [intervalMs])
    return '.'.repeat(count)
}

function PlaceholderBubble({ text }: { text: string }) {
    const dots = useEllipsisDots()
    return (
        <View style={styles.row}>
            <View style={styles.bubble}>
                <Text style={[styles.text, styles.placeholder]}>
                    {text}
                    {dots}
                </Text>
            </View>
        </View>
    )
}

export function AIMessage({ message }: Props) {
    // 订阅所属 bucket 的 phase + compressionMessage；Step 4b 接 React.memo 后再做 selector 优化
    const bucketPhase = useChatStore(
        (s) => s.messagesBySession.get(message.sid)?.streamPhase ?? 'idle'
    )
    const compressionMessage = useChatStore(
        (s) => s.messagesBySession.get(message.sid)?.compressionMessage
    )
    const isAwaitingFirstToken =
        message.status === 'streaming' && message.content.length === 0
    const placeholder = isAwaitingFirstToken
        ? placeholderForPhase(bucketPhase, compressionMessage)
        : null

    if (placeholder != null) {
        return <PlaceholderBubble text={placeholder} />
    }

    return (
        <View style={styles.row}>
            <View style={styles.bubble}>
                <Text style={styles.text}>{message.content}</Text>
            </View>
        </View>
    )
}

const styles = StyleSheet.create({
    row: {
        flexDirection: 'row',
        justifyContent: 'flex-start',
        marginVertical: 4,
    },
    bubble: {
        maxWidth: '80%',
        paddingHorizontal: 14,
        paddingVertical: 10,
        borderRadius: 18,
        borderBottomLeftRadius: 4,
        backgroundColor: '#FFFFFF',
    },
    text: {
        color: '#2B2216',
        fontSize: 16,
        lineHeight: 22,
    },
    placeholder: {
        color: '#998260',
        fontStyle: 'italic',
    },
})