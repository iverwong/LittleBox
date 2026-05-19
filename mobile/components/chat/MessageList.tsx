/**
 * M7 · 消息列表（Step 2 静态版）。
 *
 * 渲染 chatStore.messagesBySession.get(sid).messages（已固化 content）。
 * inverted FlatList：数组首位 = newest，屏幕底部，上滚触发 loadMoreMessages。
 *
 * 不在本组件触发 loadMessages 兜底拉取；首次拉取由 chat/index.tsx
 * 按 §3.10 缓存判定矩阵决定（Step 2.3 接入）。
 */
import { useCallback } from 'react'
import { ActivityIndicator, FlatList, StyleSheet, Text, View } from 'react-native'

import { AIMessage } from '@/components/chat/AIMessage'
import { HumanMessage } from '@/components/chat/HumanMessage'
import type { SessionId } from '@/services/api/chat'
import { useChatStore, type Message } from '@/stores/chat'

type Props = {
    sid: SessionId
}

export function MessageList({ sid }: Props) {
    const bucket = useChatStore((s) => s.messagesBySession.get(sid))
    const loadMoreMessages = useChatStore((s) => s.loadMoreMessages)

    const onEndReached = useCallback(() => {
        if (bucket?.hasMore) {
            void loadMoreMessages(sid)
        }
    }, [bucket?.hasMore, loadMoreMessages, sid])

    if (bucket == null) {
        return null
    }

    if (bucket.messages.length === 0) {
        return (
            <View style={styles.empty}>
                <Text style={styles.emptyText}>开始聊天吧</Text>
            </View>
        )
    }

    return (
        <FlatList<Message>
            data={bucket.messages}
            inverted
            keyExtractor={(m) => m.id}
            renderItem={({ item }) =>
                item.role === 'human' ? (
                    <HumanMessage message={item} />
                ) : (
                    <AIMessage message={item} />
                )
            }
            onEndReached={onEndReached}
            onEndReachedThreshold={0.3}
            ListFooterComponent={
                bucket.hasMore ? (
                    <View style={styles.loadingMore}>
                        <ActivityIndicator size="small" color="#998260" />
                    </View>
                ) : null
            }
            contentContainerStyle={styles.content}
        />
    )
}

const styles = StyleSheet.create({
    empty: {
        flex: 1,
        alignItems: 'center',
        justifyContent: 'center',
    },
    emptyText: {
        color: '#998260',
        fontSize: 14,
    },
    content: {
        paddingHorizontal: 16,
        paddingVertical: 12,
    },
    loadingMore: {
        paddingVertical: 16,
        alignItems: 'center',
    },
})