/**
 * M7 · 子端会话历史列表页 (/child/sessions)。
 *
 * 从原 chat 主页的内嵌历史区抽出独立路由：
 * - 顶部自绘 header（child/_layout 是 headerShown:false 的 Stack）
 * - 主体复用 SessionList 组件（onPress 行为见小步 2-4，跳详情）
 * - 左上角返回按钮 → router.back()，回到 chat 主页
 */
import { Ionicons } from '@expo/vector-icons'
import { router } from 'expo-router'
import { Pressable, StyleSheet, Text, View } from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'

import { SessionList } from '@/components/chat/SessionList'

export default function SessionsIndex() {
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
                <Text style={styles.title}>会话历史</Text>
                {/* 右侧占位，平衡左侧图标宽度，让标题视觉居中 */}
                <View style={styles.iconBtn} />
            </View>
            <View style={styles.body}>
                <SessionList />
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
    title: { fontSize: 16, color: '#998260', fontWeight: '500' },
    body: { flex: 1 },
})