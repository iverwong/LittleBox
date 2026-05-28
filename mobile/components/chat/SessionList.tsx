/**
 * M7 · 历史 session 列表。
 *
 * 列表来源：chatStore.sessions（永不含今日，由后端 sessions[] 过滤）。
 * 渲染 + 上拉加载更多；点击行为 router.push('/child/sessions/<sid>') 跳详情只读页。
 * 由 /child/sessions/index.tsx 与 /child/sessions/[sid].tsx 消费。
 */
import { router } from 'expo-router'
import { FlatList, Pressable, StyleSheet, Text, View } from 'react-native'
import { useChatStore, type SessionMeta } from '@/stores/chat'

export function SessionList() {
    const sessions = useChatStore((s) => s.sessions)
    const hasMore = useChatStore((s) => s.sessionsHasMore)
    const loadSessions = useChatStore((s) => s.loadSessions)

    if (sessions.length === 0) {
        return (
            <View style={styles.empty}>
                <Text style={styles.emptyText}>暂无历史会话</Text>
            </View>
        )
    }

    return (
        <FlatList
            data={sessions}
            keyExtractor={(item) => item.id}
            renderItem={({ item }) => <SessionListItem item={item} />}
            ListFooterComponent={
                hasMore ? (
                    <Pressable
                        style={styles.more}
                        onPress={() => {
                            void loadSessions()
                        }}
                    >
                        <Text style={styles.moreText}>加载更多</Text>
                    </Pressable>
                ) : null
            }
        />
    )
}

function SessionListItem({ item }: { item: SessionMeta }) {
    return (
        <Pressable
            style={styles.item}
            onPress={() => {
                // 跳详情页：消息历史拉取由 [sid].tsx 内的 §3.10 缓存判定矩阵兜底
                router.push(`/child/sessions/${item.id}` as never)
            }}
        >
            <Text style={styles.title}>{item.title ?? '（无标题）'}</Text>
        </Pressable>
    )
}

const styles = StyleSheet.create({
    empty: { paddingVertical: 24, alignItems: 'center' },
    emptyText: { color: '#999' },
    item: {
        paddingVertical: 12,
        paddingHorizontal: 16,
        borderBottomWidth: 1,
        borderBottomColor: '#eee',
    },
    title: { fontSize: 16 },
    more: { paddingVertical: 12, alignItems: 'center' },
    moreText: { color: '#666' },
})