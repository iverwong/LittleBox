/**
 * M7 · 子端主对话页 (/child/chat)。
 *
 * Step 1.4 范围：
 *   1. mount 时 loadSessions({ reset: true })，消费顶层 today_session_id 决定首屏
 *   2. today != null → setActiveSession(today) → 渲染消息区域占位 + 输入框占位
 *   3. today == null → 保持 activeSessionId=null → 渲染 WelcomeShell
 *   4. 顶部渲染 SessionList（历史 session）
 *
 * 后续 Step：消息列表（Step 2）、ChatInput（Step 3）、SSE sendMessage（Step 4a）。
 */
import { useEffect, useState } from 'react'
import { ActivityIndicator, StyleSheet, Text, View } from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'

import { MessageList } from '@/components/chat/MessageList'
import { SessionList } from '@/components/chat/SessionList'
import { WelcomeContent } from '@/components/chat/WelcomeContent'
import { WelcomeShell } from '@/components/chat/WelcomeShell'
import { useChatStore } from '@/stores/chat'

export default function ChatIndex() {
    const loadSessions = useChatStore((s) => s.loadSessions)
    const setActiveSession = useChatStore((s) => s.setActiveSession)
    const activeSessionId = useChatStore((s) => s.activeSessionId)

    useEffect(() => {
        let cancelled = false
        void (async () => {
            try {
                await loadSessions({ reset: true })
                if (cancelled) return
                const today = useChatStore.getState().todaySessionId
                if (today != null) {
                    setActiveSession(today)
                }
            } catch (err) {
                // Step 7 接错误码映射 UI 反馈
                console.error('[ChatIndex] loadSessions failed', err)
            }
        })()
        return () => {
            cancelled = true
        }
    }, [loadSessions, setActiveSession])

    const [isLoadingMessages, setIsLoadingMessages] = useState(false)
    const loadMessages = useChatStore((s) => s.loadMessages)

    // §3.10 缓存判定矩阵：activeSessionId 变化时决定是否拉消息历史
    // - 有 ActiveStream（Step 4a 写入）→ 直接用 store 渲染
    // - 有缓存 + lastFetchedAt < 30s → 走缓存
    // - 否则 → 整页 loading + 一次 GET
    useEffect(() => {
        if (activeSessionId == null) return

        const state = useChatStore.getState()
        const bucket = state.messagesBySession.get(activeSessionId)
        const hasActiveStream = state.activeStreams.has(activeSessionId)

        if (hasActiveStream) return
        if (bucket != null && Date.now() - bucket.lastFetchedAt < 30_000) return

        let cancelled = false
        setIsLoadingMessages(true)
        void (async () => {
            try {
                await loadMessages(activeSessionId)
            } catch (err) {
                // Step 7 接错误码映射 UI 反馈
                console.error('[ChatIndex] loadMessages failed', err)
            } finally {
                if (!cancelled) setIsLoadingMessages(false)
            }
        })()
        return () => {
            cancelled = true
        }
    }, [activeSessionId, loadMessages])

    return (
        <SafeAreaView style={styles.container} edges={['top', 'bottom']}>
            <View style={styles.history}>
                <Text style={styles.historyHeader}>会话历史</Text>
                <SessionList />
            </View>

            <View style={styles.main}>
                {activeSessionId == null ? (
                    <WelcomeShell content={<WelcomeContent />} />
                ) : isLoadingMessages ? (
                    <View style={styles.loading}>
                        <ActivityIndicator size="large" color="#998260" />
                    </View>
                ) : (
                    <MessageList sid={activeSessionId} />
                )}
            </View>

            <View style={styles.inputPlaceholder}>
                <Text style={styles.placeholderText}>输入框占位（Step 3 接 ChatInput）</Text>
            </View>
        </SafeAreaView>
    )
}

const styles = StyleSheet.create({
    container: { flex: 1, backgroundColor: '#F2EADF' },
    history: {
        maxHeight: 200,
        borderBottomWidth: 1,
        borderBottomColor: '#E5DBC9',
        backgroundColor: 'rgba(255,255,255,0.4)',
    },
    historyHeader: {
        paddingHorizontal: 16,
        paddingTop: 12,
        paddingBottom: 4,
        fontSize: 14,
        color: '#998260',
        fontWeight: '500',
    },
    main: { flex: 1 },
    loading: { flex: 1, alignItems: 'center', justifyContent: 'center' },
    placeholderText: { color: '#998260' },
    inputPlaceholder: {
        padding: 16,
        borderTopWidth: 1,
        borderTopColor: '#E5DBC9',
        alignItems: 'center',
    },
})