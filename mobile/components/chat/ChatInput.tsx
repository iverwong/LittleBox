/**
 * M7 · 子端聊天输入框组件。
 *
 * Step 3.1：草稿态（受控 TextInput + Mascot + send 按钮）
 * Step 4a.2：流式态 — isStreaming=true 时 send 图标切 stop，点击行为 stub
 *            （真停止接口 Step 5 接入，触发 stopStream(activeSessionId)）
 * Step 6：A4 态右侧增「重新生成」按钮（紧邻发送按钮）
 *
 * 设计约束（M7 §3.10）：流式中输入框仍可继续打字（草稿保留），仅按钮语义切换。
 */
import { Ionicons } from '@expo/vector-icons'
import { useState } from 'react'
import { Pressable, StyleSheet, TextInput, View } from 'react-native'

import { Mascot } from '@/components/mascot/Mascot'

type ChatInputProps = {
    onSend: (content: string) => void
    /**
     * 当前活跃 session 是否处于流式回复中。
     * - false（默认）：发送按钮显示 arrow-up；trim 后空内容置灰
     * - true：发送按钮显示 stop；点击行为 Step 4a.2 暂为 stub（Step 5 接真停止）
     */
    isStreaming?: boolean
}

export function ChatInput({ onSend, isStreaming = false }: ChatInputProps) {
    const [value, setValue] = useState('')
    const trimmed = value.trim()
    const canSend = !isStreaming && trimmed.length > 0
    // streaming 态下 stop 按钮始终可点；非 streaming 态按 canSend 决定
    const canPress = isStreaming || canSend

    const handlePress = () => {
        if (isStreaming) {
            // Step 5 接入真停止：useChatStore.getState().stopStream(activeSessionId)
            console.log('[ChatInput] stop pressed (stub, Step 5 待接入)')
            return
        }
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
                multiline
                maxLength={2000}
            />
            <Pressable
                onPress={handlePress}
                accessibilityLabel={isStreaming ? '停止' : '发送'}
                disabled={!canPress}
                style={({ pressed }) => [
                    styles.sendBtn,
                    canPress ? styles.sendBtnActive : styles.sendBtnDisabled,
                    pressed && canPress && styles.sendBtnPressed,
                ]}
            >
                <Ionicons
                    name={isStreaming ? 'stop' : 'arrow-up'}
                    size={20}
                    color={canPress ? '#FFFFFF' : '#998260'}
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