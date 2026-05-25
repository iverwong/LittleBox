/**
 * M7 · AI 消息气泡。
 *
 * Step 2 静态版：仅渲染已固化 content
 * Step 4a.3：流式「首 token 未到」态消费 bucket.streamPhase 渲染占位文案 + 动态省略号
 * Step 4b：叠加 useStreamBuffer（组件级 50ms flush）+ React.memo 隔离 re-render
 * Step 5：根据 stoppedTag 渲染「已停止」角标
 * Step 6：根据 status='failed' 渲染 A4 失败占位
 *
 * M7-patch · M8-patch 9 事件契约对齐（2026-05）：
 * - 'compressing' 占位文案锁定 B 案：「正在为对话腾出更多空间」（不带「…」，由 dots 动态提供）
 * - 移除 compressionMessage 订阅（store 已删字段；新契约 compression_start/end payload 为空 {}）
 * - phase 转换：compression_start → 'compressing' / compression_end → 'feeling' / thinking_start → 'thinking'
 * - 占位文案 4 段过渡节奏：feeling → compressing → feeling → thinking
 *
 * 设计约束：
 * - streamPhase 是 bucket(SessionMessageState)级状态，不是 Message 字段
 * - 仅当 AI 气泡 status='streaming' && content='' 时消费 phase；首 delta 到达即覆盖占位
 * - 'feeling' phase 当前 SSE 协议未发，文案映射保留为 forward-compat（Step 6 mascot 接入时启用）
 *
 * Step 4b 关键设计：
 * - phase 订阅下沉到 StreamingPlaceholder 子组件，committed / streaming-with-content
 *   路径不订阅 bucket.streamPhase，避免同 sid 内无关 AI 气泡的连带 re-render
 * - useStreamBuffer 始终在 AI 气泡组件挂载期间常驻；仅 status='streaming' 时启用 sink
 * - 已固化消息走 React.memo 浅比较（message ref 未变 → 跳过 re-render）
 */
import { Ionicons } from '@expo/vector-icons'
import { memo, useCallback, useEffect, useState } from 'react'
import { StyleSheet, Text, TouchableOpacity, View } from 'react-native'

import { useStreamBuffer } from '@/hooks/useStreamBuffer'
import type { Message, StreamPhase } from '@/stores/chat'
import { useChatStore } from '@/stores/chat'

type Props = {
    message: Message
}

function placeholderForPhase(phase: StreamPhase): string | null {
    switch (phase) {
        case 'feeling':
            return '感受中'
        case 'compressing':
            // M7-patch: B 案锁定文案（不带「…」，dots 动态追加）
            return '正在为对话腾出更多空间'
        case 'thinking':
            return '思考中'
        default:
            // 'idle' / 'delta' / 'interrupted' 不显示文案占位
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

/**
 * Step 4b · 占位气泡子组件。phase 订阅下沉至此，
 * 仅在 AI 气泡处于「streaming + content 为空」时挂载。
 * 这样同 sid 内已固化气泡与正在打字的气泡都不会因为 phase 变化触发 re-render。
 */
function StreamingPlaceholder({ sid }: { sid: string }) {
    const phase = useChatStore(
        (s) => s.messagesBySession.get(sid)?.streamPhase ?? 'idle',
    )
    const text = placeholderForPhase(phase)
    const dots = useEllipsisDots()
    if (text == null) return null
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

function AIMessageImpl({ message }: Props) {
    const appendFlushedDelta = useChatStore((s) => s._appendFlushedDelta)
    const regenerate = useChatStore((s) => s.regenerate)
    const isLastAi = useChatStore((s) => {
        const bucket = s.messagesBySession.get(message.sid)
        if (!bucket) return false
        // inverted FlatList：messages[0] = newest，从前往后找第一条 ai 即末位 AI
        for (const m of bucket.messages) {
            if (m.role === 'ai') return m.id === message.id
        }
        return false
    })
    const isStreaming = message.status === 'streaming'

    const handleRegenerate = useCallback(() => {
        void regenerate(message.sid)
    }, [regenerate, message.sid])

    const onFlush = useCallback(
        (chunk: string) => {
            appendFlushedDelta(message.sid, message.id, chunk)
        },
        [appendFlushedDelta, message.sid, message.id],
    )

    // 始终调用 hook 保证 hook 顺序稳定；enabled 控制 sink/timer 是否启用
    useStreamBuffer({
        sid: message.sid,
        enabled: isStreaming,
        onFlush,
    })

    if (isStreaming && message.content.length === 0) {
        return <StreamingPlaceholder sid={message.sid} />
    }

    if (message.status === 'failed') {
        // Step 6 · A4 失败态：bubble 内显示「⚠ 回复失败」（保留 partial content 如有），
        // bubble 右侧外挂「重新生成」chip 按钮（同一行）。
        // 点击 chip → chatStore.regenerate(sid) → 走后端决策矩阵 Row 6（复用孤儿 human）。
        // 点击瞬间 status='failed' → 'streaming'，本组件下一帧渲染 StreamingPlaceholder 接管。
        return (
            <View style={styles.row}>
                <View style={styles.bubble}>
                    {message.content.length > 0 && (
                        <Text style={styles.text}>{message.content}</Text>
                    )}
                    <View style={styles.failedTag}>
                        <Ionicons name="alert-circle-outline" size={12} color="#C26B6B" />
                        <Text style={styles.failedText}>回复失败</Text>
                    </View>
                </View>
                {isLastAi && (
                    <TouchableOpacity
                        style={styles.regenerateChip}
                        onPress={handleRegenerate}
                        activeOpacity={0.7}
                    >
                        <Ionicons name="refresh" size={14} color="#7A6A4F" />
                        <Text style={styles.regenerateText}>重新生成</Text>
                    </TouchableOpacity>
                )}
            </View>
        )
    }

    return (
        <View style={styles.row}>
            <View style={styles.bubble}>
                <Text style={styles.text}>
                    {message.content}
                </Text>
                {message.stoppedTag && (
                    <View style={styles.stoppedTag}>
                        <Ionicons name="stop-circle-outline" size={12} color="#998260" />
                        <Text style={styles.stoppedText}>已停止</Text>
                    </View>
                )}
            </View>
        </View>
    )
}

/**
 * React.memo 隔离：未变更的已固化消息不会因为同列表的流式消息 re-render
 * 而连带刷新。store 对每条变更消息都会产出新 ref，shallow 比较即可生效。
 */
export const AIMessage = memo(AIMessageImpl)

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
    stoppedTag: {
        flexDirection: 'row',
        alignItems: 'center',
        marginTop: 6,
        paddingTop: 6,
        borderTopWidth: StyleSheet.hairlineWidth,
        borderTopColor: '#E5DBC9',
        gap: 4,
        alignSelf: 'flex-start',
    },
    stoppedText: {
        fontSize: 12,
        color: '#998260',
        fontStyle: 'italic',
    },
    failedTag: {
        flexDirection: 'row',
        alignItems: 'center',
        marginTop: 4,
        gap: 4,
        alignSelf: 'flex-start',
    },
    failedText: {
        fontSize: 12,
        color: '#C26B6B',
        fontStyle: 'italic',
    },
    regenerateChip: {
        flexDirection: 'row',
        alignItems: 'center',
        alignSelf: 'flex-end',
        marginLeft: 8,
        paddingHorizontal: 10,
        paddingVertical: 6,
        borderRadius: 14,
        backgroundColor: '#F5EBD7',
        borderWidth: StyleSheet.hairlineWidth,
        borderColor: '#E5DBC9',
        gap: 4,
    },
    regenerateText: {
        fontSize: 12,
        color: '#7A6A4F',
        fontWeight: '500',
    },
})