/**
 * M7 · 子端历史会话详情回顾页 (/child/sessions/[sid])。
 *
 * - 顶部自绘 header：返回按钮 + session.title（后端已返回「周X · M月D日」格式，前端不重算）
 * - 主体：MessageList sid={sid} —— 只读态，不挂 ChatInput
 * - 底部长条主按钮：「回到今天继续聊」→ setActiveSession(todaySessionId ?? null) + router.replace('/child/chat')
 *
 * 边界：
 * - sid === todaySessionId：redirect 回 chat（避免列表点到今天 session 走错路径）
 * - todaySessionId == null：清空 activeSessionId 触发 WelcomeShell 兜底
 *
 * 消息拉取按 §3.10 缓存判定矩阵：有 activeStream / 30s 内拉过 → 直用缓存，否则整页 loading + 一次 GET。
 * 历史 session 不应处于流式，故不调 resumeOnEnter（仅 chat 主页对 today 触发）。
 */
import { Ionicons } from '@expo/vector-icons'
import { router, useLocalSearchParams } from 'expo-router'
import { useEffect, useState } from 'react'
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'

import { MessageList } from '@/components/chat/MessageList'
import { useChatErrorHandler } from '@/hooks/useChatErrorHandler'
import { useChatStore } from '@/stores/chat'

export default function SessionDetail() {
    const params = useLocalSearchParams<{ sid: string }>()
    const sid = params.sid ?? null

    const todaySessionId = useChatStore((s) => s.todaySessionId)
    const setActiveSession = useChatStore((s) => s.setActiveSession)
    const loadMessages = useChatStore((s) => s.loadMessages)
    const sessionTitle = useChatStore((s) =>
        sid == null ? null : s.sessions.find((x) => x.id === sid)?.title ?? null,
    )

    const { handleApiError } = useChatErrorHandler()
    const [isLoadingMessages, setIsLoadingMessages] = useState(false)

    // 边界：进入详情页时若 sid 命中 todaySessionId，立刻 redirect 回 chat 主页
    useEffect(() => {
        if (sid != null && todaySessionId != null && sid === todaySessionId) {
            router.replace('/child/chat')
        }
    }, [sid, todaySessionId])

    // §3.10 缓存判定矩阵（移植自 chat/index.tsx）：
    // - 有 ActiveStream → 直接用 store（历史 session 理论上不会命中，但兜底）
    // - 有缓存 + lastFetchedAt < 30s → 走缓存
    // - 否则 → 整页 loading + 一次 GET
    useEffect(() => {
        if (sid == null) return
        if (todaySessionId != null && sid === todaySessionId) return // 被 redirect

        const state = useChatStore.getState()
        const bucket = state.messagesBySession.get(sid)
        const hasActiveStream = state.activeStreams.has(sid)
        if (hasActiveStream) return
        if (bucket != null && Date.now() - bucket.lastFetchedAt < 30_000) return

        let cancelled = false
        setIsLoadingMessages(true)
        void (async () => {
            try {
                await loadMessages(sid)
            } catch (err) {
                console.error('[SessionDetail] loadMessages failed', err)
                handleApiError(err, { kind: 'loadMessages', sid })
            } finally {
                if (!cancelled) setIsLoadingMessages(false)
            }
        })()
        return () => {
            cancelled = true
        }
    }, [sid, todaySessionId, loadMessages, handleApiError])

    const handleBackToToday = () => {
        if (todaySessionId != null) {
            setActiveSession(todaySessionId)
        } else {
            // 边界：跨日且今日尚未发消息 → 清空 activeSessionId 触发 WelcomeShell
            useChatStore.setState({ activeSessionId: null })
        }
        router.replace('/child/chat')
    }

    if (sid == null) {
        // 参数缺失兜底：直接退回上一页
        return (
            <SafeAreaView style={styles.container} edges={['top', 'bottom']}>
                <View style={styles.loading}>
                    <Text style={styles.emptyText}>会话参数缺失</Text>
                </View>
            </SafeAreaView>
        )
    }

    return (
        <SafeAreaView style={styles.container} edges={['top', 'bottom']}>
            <View style={styles.header}>
                <Pressable
                    accessibilityRole="button"
                    accessibilityLabel="返回"
                    hitSlop={8}
                    onPress={() => router.back()}
                    style={({ pressed }) => [
                        styles.iconBtn,
                        pressed && styles.iconBtnPressed,
                    ]}
                >
                    <Ionicons name="chevron-back" size={24} color="#998260" />
                </Pressable>
                <Text style={styles.title} numberOfLines={1}>
                    {sessionTitle ?? '历史会话'}
                </Text>
                <View style={styles.iconBtn} />
            </View>

            <View style={styles.main}>
                {isLoadingMessages ? (
                    <View style={styles.loading}>
                        <ActivityIndicator />
                    </View>
                ) : (
                    <MessageList sid={sid} />
                )}
            </View>

            <View style={styles.footer}>
                <Pressable
                    accessibilityRole="button"
                    accessibilityLabel="回到今天继续聊"
                    onPress={handleBackToToday}
                    style={({ pressed }) => [
                        styles.backToTodayBtn,
                        pressed && styles.backToTodayBtnPressed,
                    ]}
                >
                    <Text style={styles.backToTodayText}>回到今天继续聊</Text>
                </Pressable>
            </View>
        </SafeAreaView>
    )
}

const styles = StyleSheet.create({
    container: { flex: 1, backgroundColor: '#F2EADF' },
    header: {
        flexDirection: 'row',
        alignItems: 'center',
        justifyContent: 'space-between',
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
    title: {
        flex: 1,
        textAlign: 'center',
        fontSize: 16,
        color: '#998260',
        fontWeight: '500',
        paddingHorizontal: 8,
    },
    main: { flex: 1 },
    loading: { flex: 1, alignItems: 'center', justifyContent: 'center' },
    emptyText: { color: '#998260', fontSize: 14 },
    footer: {
        paddingHorizontal: 16,
        paddingVertical: 12,
        borderTopWidth: 1,
        borderTopColor: '#E5DBC9',
        backgroundColor: '#F2EADF',
    },
    backToTodayBtn: {
        height: 48,
        borderRadius: 24,
        backgroundColor: '#998260',
        alignItems: 'center',
        justifyContent: 'center',
    },
    backToTodayBtnPressed: { opacity: 0.7 },
    backToTodayText: {
        color: '#FFFFFF',
        fontSize: 16,
        fontWeight: '500',
    },
})