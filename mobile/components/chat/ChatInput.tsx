/**
 * M7 · 子端聊天输入框组件（草稿态）。
 *
 * Step 3.1 范围：
 * 1. 受控多行 TextInput + 发送图标按钮 + 左侧静态 Mascot
 * 2. trim 后空内容 → 发送按钮置灰 + 不触发 onSend
 * 3. 点击发送 → 调 props.onSend(content) + 清空输入
 *
 * Step 3.1 本步：onSend 由父组件传 console.log；Step 4a 接入 chatStore.sendMessage。
 * Step 4a 引入流式态：发送按钮 → 停止按钮（icon 切 + 行为切）。
 * Step 6 引入 A4 态：右侧增「重新生成」按钮（紧邻发送按钮）。
 */
import { Ionicons } from '@expo/vector-icons'
import { useState } from 'react'
import { Pressable, StyleSheet, TextInput, View } from 'react-native'

import { Mascot } from '@/components/mascot/Mascot'

type ChatInputProps = {
    onSend: (content: string) => void
}

export function ChatInput({ onSend }: ChatInputProps) {
    const [value, setValue] = useState('')
    const trimmed = value.trim()
    const canSend = trimmed.length > 0

    const handlePress = () => {
        if (!canSend) return
        onSend(trimmed)
        setValue('')
    }

    return (
        <View style={styles.container}>
            <View style={styles.mascotSlot}>
                <Mascot size="sm" />
            </View>
            <TextInput
                style={styles.input}
                value={value}
                onChangeText={setValue}
                placeholder="说点什么…"
                placeholderTextColor="#B8A985"
                multiline
                maxLength={2000}
                textAlignVertical="center"
            />
            <Pressable
                accessibilityRole="button"
                accessibilityLabel="发送"
                accessibilityState={{ disabled: !canSend }}
                onPress={handlePress}
                hitSlop={8}
                style={({ pressed }) => [
                    styles.sendBtn,
                    canSend ? styles.sendBtnActive : styles.sendBtnDisabled,
                    pressed && canSend && styles.sendBtnPressed,
                ]}
            >
                <Ionicons
                    name="arrow-up"
                    size={20}
                    color={canSend ? '#FFFFFF' : '#D7CDB6'}
                />
            </Pressable>
        </View>
    )
}

const styles = StyleSheet.create({
    container: {
        flexDirection: 'row',
        alignItems: 'flex-end',
        paddingHorizontal: 12,
        paddingVertical: 10,
        gap: 8,
        borderTopWidth: 1,
        borderTopColor: '#E5DBC9',
        backgroundColor: '#F2EADF',
    },
    mascotSlot: {
        paddingBottom: 4,
    },
    input: {
        flex: 1,
        minHeight: 40,
        maxHeight: 120,
        paddingHorizontal: 12,
        paddingVertical: 8,
        borderRadius: 20,
        backgroundColor: '#FFFFFF',
        fontSize: 16,
        color: '#2B2216',
    },
    sendBtn: {
        width: 36,
        height: 36,
        borderRadius: 18,
        alignItems: 'center',
        justifyContent: 'center',
    },
    sendBtnActive: {
        backgroundColor: '#998260',
    },
    sendBtnDisabled: {
        backgroundColor: '#EFE6D5',
    },
    sendBtnPressed: {
        opacity: 0.7,
    },
})