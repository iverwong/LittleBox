/**
 * M7 · 子端主对话页 (/child/chat)。
 *
 * 当前实现累计范围：
 * - Step 1.4：mount loadSessions + today_session_id 决定首屏 + 顶部 SessionList
 * - Step 2.3：§3.10 缓存判定矩阵（30s 窗口）+ MessageList 渲染
 * - Step 3.2：输入区按 activeSessionId vs todaySessionId 三分支
 *   · null → WelcomeShell 占满（不显示输入区）
 *   · === todaySessionId → ChatInput 草稿态
 *   · !== todaySessionId（历史）→ 「返回继续对话」图标按钮
 * - Step 7：挂 useChatErrorHandler hook（接 store transport error / stopStream 失败回调）
 *          两处 loadSessions / loadMessages catch 接 handleApiError（按 §3.9 分发 toast / 切 session）
 *          ChatInput 接入 pendingPrefill 链路（A4Late 首帧超时回灌）
 *
 * 后续 Step：Resume 三分支决策器（Step 8）。
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
import { useChatErrorHandler } from '@/hooks/useChatErrorHandler'
import { useChatStore } from '@/stores/chat'

export default function ChatIndex() {
    const loadSessions = useChatStore((s) => s.loadSessions)
    const setActiveSession = useChatStore((s) => s.setActiveSession)
    const activeSessionId = useChatStore((s) => s.activeSessionId)
    const todaySessionId = useChatStore((s) => s.todaySessionId)
    const sendMessage = useChatStore((s) => s.sendMessage)
    const stopStream = useChatStore((s) => s.stopStream)
    const pendingPrefill = useChatStore((s) => s.pendingPrefill)
    const setPendingPrefill = useChatStore((s) => s.setPendingPrefill)
    const resumeOnEnter = useChatStore((s) => s.resumeOnEnter)

    // Step 7 · 错误反馈映射 hook
    // - mount 时 setOnChatErrorHandler 注册（接管 store transport error / stopStream 失败回调）
    // - 暴露 handleApiError：loadSessions / loadMessages catch 块按 §3.9 分发
    // - unmount 时清空 callback 避免 stale ref
    const { handleApiError } = useChatErrorHandler()

    useEffect(() => {
        let cancelled = false
        void (async () => {
            try {
                await loadSessions({ reset: true })
                if (cancelled) return
                const today = useChatStore.getState().todaySessionId
                if (today == null) return
                setActiveSession(today)

                // Step 8 · 接通 resumeOnEnter
                // setActiveSession(today) 触发 §3.10 useEffect → loadMessages（limit:50）；
                // 同时 resumeOnEnter 并发 GET messages（limit:1）做决策，分支侧效在 store 内：
                // - OK2 / Active：不做事，bucket 由 §3.10 兜底
                // - Waiting：_startResumePolling 接管（每 2s 决策 + fabricate streaming 槽）
                // - A4Late：_fabricateA4LateSlot unshift failed 槽（A4 失败卡 + 重新生成 chip）
                try {
                    const branch = await resumeOnEnter(today)
                    if (cancelled) return
                    if (branch.type === 'Active') {
                        console.warn('[ChatIndex] resumeOnEnter returned Active on mount (unexpected)')
                    }
                } catch (err) {
                    console.error('[ChatIndex] resumeOnEnter failed', err)
                    handleApiError(err, { kind: 'loadMessages', sid: today })
                }
            } catch (err) {
                console.error('[ChatIndex] loadSessions failed', err)
                // Step 7 · 按 §3.9 状态码分发；非 ApiError 重新抛出到 ErrorBoundary
                handleApiError(err, { kind: 'loadSessions' })
            }
        })()
        return () => {
            cancelled = true
        }
    }, [loadSessions, setActiveSession, resumeOnEnter, handleApiError])

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
                console.error('[ChatIndex] loadMessages failed', err)
                // Step 7 · 403/404 → 切 session + reset；5xx → toast；0 → 网络异常 toast
                handleApiError(err, { kind: 'loadMessages', sid: activeSessionId })
            } finally {
                if (!cancelled) setIsLoadingMessages(false)
            }
        })()
        return () => {
            cancelled = true
        }
    }, [activeSessionId, loadMessages, handleApiError])

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
                    prefill={pendingPrefill}
                    onPrefillConsumed={() => setPendingPrefill(null)}
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