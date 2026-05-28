/**
 * M7 · 子端主对话页 (/child/chat)。
 *
 * 当前实现累计范围：
 * - Step 1.4：mount loadSessions + today_session_id 决定首屏
 * - Step 2.3：§3.10 缓存判定矩阵（30s 窗口）+ MessageList 渲染
 * - Step 7：挂 useChatErrorHandler hook（接 store transport error / stopStream 失败回调）
 *          两处 loadSessions / loadMessages catch 接 handleApiError（按 §3.9 分发 toast / 切 session）
 *          ChatInput 接入 pendingPrefill 链路（A4Late 首帧超时回灌）
 * - Step 8：mount 路径接通 resumeOnEnter（与 §3.10 useEffect 并发，分支侧效在 store 内）
 * - Step 9：isOffline 派生（bucket[0].status === 'reconnecting' | 'disconnected'）下传 ChatInput
 * - FE UI 优化阶段：
 *   · 历史会话列表抽到 /child/sessions（左上角 time-outline 图标入口）
 *   · 历史 session 详情抽到 /child/sessions/[sid]（含底部「回到今天继续聊」长条主按钮）
 *   · 主页输入区从 today / history / null 三分支收成 today / null 两分支：
 *     - null → WelcomeShell 占满（不显示输入区）
 *     - === todaySessionId → ChatInput 草稿态（含 isStreaming / isOffline 派生）
 *   · 键盘抬起方案 A：react-native-keyboard-controller 的 KeyboardProvider + 库版 KeyboardAvoidingView
 */
import { Ionicons } from '@expo/vector-icons'
import { router } from 'expo-router'
import { useEffect, useState } from 'react'
import { ActivityIndicator, Pressable, StyleSheet, View } from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'
import { KeyboardAvoidingView } from 'react-native-keyboard-controller'

import { ChatInput } from '@/components/chat/ChatInput'
import { MessageList } from '@/components/chat/MessageList'
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

    // 「可写态」包含 today==active==null（WelcomeShell + 首条消息触发隐式建 session）
    // 注：historyActive 分支已抽到 /child/sessions/[sid] 详情页，主页只剩 today / null 两态
    const isTodayActive = activeSessionId === todaySessionId
    // Step 4a.2：当前活跃 session 是否在流式回复中 — 下传 ChatInput 切 stop 按钮
    const isStreaming = useChatStore((s) =>
        activeSessionId != null && s.activeStreams.has(activeSessionId)
    )

    // Step 9 · 当前活跃 session 是否离线态(reconnecting / disconnected)。
    // 判定 bucket[0].status 是否落在二态中。与 isStreaming 互斥。
    const isOffline = useChatStore((s) => {
        if (activeSessionId == null) return false
        const head = s.messagesBySession.get(activeSessionId)?.messages[0]
        if (!head || head.role !== 'ai') return false
        return head.status === 'reconnecting' || head.status === 'disconnected'
    })

    return (
        <SafeAreaView style={styles.container} edges={['top', 'bottom']}>
            <KeyboardAvoidingView behavior="padding" style={styles.kav}>
                <View style={styles.topBar}>
                    <Pressable
                        accessibilityRole="button"
                        accessibilityLabel="查看会话历史"
                        hitSlop={8}
                        onPress={() => router.push('/child/sessions' as never)}
                        style={({ pressed }) => [
                            styles.iconBtn,
                            pressed && styles.iconBtnPressed,
                        ]}
                    >
                        <Ionicons name="time-outline" size={24} color="#998260" />
                    </Pressable>
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
                        isOffline={isOffline}
                    />
                )}
            </KeyboardAvoidingView>
        </SafeAreaView>
    )
}

const styles = StyleSheet.create({
    container: { flex: 1, backgroundColor: '#F2EADF' },
    kav: { flex: 1 },
    topBar: {
        flexDirection: 'row',
        alignItems: 'center',
        paddingHorizontal: 12,
        paddingVertical: 8,
        borderBottomWidth: 1,
        borderBottomColor: '#E5DBC9',
    },
    iconBtn: {
        width: 40,
        height: 40,
        alignItems: 'center',
        justifyContent: 'center',
    },
    iconBtnPressed: { opacity: 0.6 },
    main: { flex: 1 },
    loading: { flex: 1, alignItems: 'center', justifyContent: 'center' },
})