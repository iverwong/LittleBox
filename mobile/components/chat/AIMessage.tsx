/**
 * M7 · AI 消息气泡（Step 2 静态版）。
 *
 * 左对齐、浅色背景深字，仅渲染已固化 content。
 * Step 4b 将在本组件叠加 useStreamBuffer，AI 流式态采用「已固化 content + buffer chunk」双源。
 * Step 5 将根据 stoppedTag 渲染「已停止」角标；Step 6 将根据 status='failed' 渲染 A4 失败占位。
 */
import { StyleSheet, Text, View } from 'react-native'

import type { Message } from '@/stores/chat'

type Props = {
    message: Message
}

export function AIMessage({ message }: Props) {
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
})