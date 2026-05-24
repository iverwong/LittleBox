/**
 * M7 · 子端主对话页 (/child/chat)。
 *
 * 当前实现累计范围：
 *   - Step 1.4：mount loadSessions + today_session_id 决定首屏 + 顶部 SessionList
 *   - Step 2.3：§3.10 缓存判定矩阵（30s 窗口）+ MessageList 渲染
 *   - Step 3.2：输入区按 activeSessionId vs todaySessionId 三分支
 *       · null → WelcomeShell 占满（不显示输入区）
 *       · === todaySessionId → ChatInput 草稿态
 *       · !== todaySessionId（历史）→ 「返回继续对话」图标按钮
 *
 * 后续 Step：SSE sendMessage（Step 4a）、token buffer（Step 4b）、Resume（Step 8）。
 */
import { Ionicons } from '@expo/vector-icons'
import { useEffect, useState } from 'react'
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'

import { ChatInput } from '@/components/chat/ChatInput'
import { MessageList } from '@/components/chat/MessageList'
import { SessionList } from '@/components/chat/SessionList'
import { WelcomeContent } from '@/components/chat/WelcomeContent'
import { WelcomeShell } from '@/components/chat/WelcomeShell'
import { useChatStore } from '@/stores/chat'

export default function ChatIndex() {
    const loadSessions = useChatStore((s) => s.loadSessions)
    const setActiveSession = useChatStore((s) => s.setActiveSession)
    const activeSessionId = useChatStore((s) => s.activeSessionId)
    const todaySessionId = useChatStore((s) => s.todaySessionId)
    const sendMessage = useChatStore((s) => s.sendMessage)
    const stopStream = useChatStore((s) => s.stopStream)

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

    const handleBackToToday = () => {
        if (todaySessionId != null) {
            setActiveSession(todaySessionId)
        } else {
            // 边界：跨日且今日尚未发消息 → 清空 activeSessionId 触发 WelcomeShell
            useChatStore.setState({ activeSessionId: null })
        }
    }

    // 「可写态」包含 today==active==null（WelcomeShell + 首条消息触发隐式建 session）
    const isTodayActive = activeSessionId === todaySessionId
    const isHistoryActive = activeSessionId != null && activeSessionId !== todaySessionId
    // Step 4a.2：当前活跃 session 是否在流式回复中 — 下传 ChatInput 切 stop 按钮
    const isStreaming = useChatStore((s) =>
        activeSessionId != null && s.activeStreams.has(activeSessionId)
    )

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

            {isTodayActive && (
                <ChatInput
                    onSend={(content) => {
                        void sendMessage(activeSessionId, content)
                    }}
                    isStreaming={isStreaming}
                    onStop={() => {
                        if (activeSessionId != null) {
                            void stopStream(activeSessionId)
                        }
                    }}
                />
            )}
            {isHistoryActive && (
                <View style={styles.backToTodayBar}>
                    <Pressable
                        accessibilityRole="button"
                        accessibilityLabel="返回继续对话"
                        onPress={handleBackToToday}
                        hitSlop={8}
                        style={({ pressed }) => [
                            styles.backToTodayBtn,
                            pressed && styles.backToTodayBtnPressed,
                        ]}
                    >
                        <Ionicons name="return-down-back" size={20} color="#FFFFFF" />
                    </Pressable>
                </View>
            )}
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
    backToTodayBar: {
        flexDirection: 'row',
        justifyContent: 'center',
        alignItems: 'center',
        paddingVertical: 12,
        borderTopWidth: 1,
        borderTopColor: '#E5DBC9',
        backgroundColor: '#F2EADF',
    },
    backToTodayBtn: {
        width: 40,
        height: 40,
        borderRadius: 20,
        backgroundColor: '#998260',
        alignItems: 'center',
        justifyContent: 'center',
    },
    backToTodayBtnPressed: {
        opacity: 0.7,
    },
})