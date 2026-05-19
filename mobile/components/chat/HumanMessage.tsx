/**
 * M7 · 用户消息气泡（Step 2 静态版）。
 *
 * 右对齐、深色背景白字。M4.5 token 待对齐，先用 #998260 / #FFFFFF 占位色。
 */
import { StyleSheet, Text, View } from 'react-native'

import type { Message } from '@/stores/chat'

type Props = {
    message: Message
}

export function HumanMessage({ message }: Props) {
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
        justifyContent: 'flex-end',
        marginVertical: 4,
    },
    bubble: {
        maxWidth: '80%',
        paddingHorizontal: 14,
        paddingVertical: 10,
        borderRadius: 18,
        borderBottomRightRadius: 4,
        backgroundColor: '#998260',
    },
    text: {
        color: '#FFFFFF',
        fontSize: 16,
        lineHeight: 22,
    },
})