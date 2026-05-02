/**
 * parent/children/[id]/settings.tsx — 孩子设置占位页（M5 F5.6）。
 *
 * 仅占位 — M6+ 接入修改 nickname / 年龄 / 性别等表单。本页不调任何 API。
 * 视觉范式与 new.tsx 一致：米杏底色 + 自画返回栏 + SafeAreaView。
 */
import { Stack, useLocalSearchParams, useRouter } from 'expo-router'
import { Pressable, StyleSheet, Text, View } from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'
import { Ionicons } from '@expo/vector-icons'
import { useTheme } from '@/theme'

export default function ChildSettingsScreen() {
    const theme = useTheme()
    const router = useRouter()
    const { id } = useLocalSearchParams<{ id: string }>()

    return (
        <>
            <Stack.Screen options={{ headerShown: false }} />
            <SafeAreaView
                style={[styles.root, { backgroundColor: theme.surface.paper }]}
                edges={['top', 'left', 'right']}
            >
                <View style={styles.topBar}>
                    <Pressable
                        onPress={() => router.back()}
                        hitSlop={12}
                        style={({ pressed }) => [
                            styles.backButton,
                            { opacity: pressed ? 0.5 : 1 },
                        ]}
                        accessibilityRole="button"
                        accessibilityLabel="返回"
                    >
                        <Ionicons
                            name="chevron-back"
                            size={24}
                            color={theme.palette.secondary[600]}
                        />
                    </Pressable>
                    <Text
                        style={[styles.topTitle, { color: theme.palette.neutral[900] }]}
                        numberOfLines={1}
                    >
                        孩子设置
                    </Text>
                    {/* 占位让 title 居中（与左侧返回按钮对称） */}
                    <View style={styles.backButton} />
                </View>

                <View style={styles.content}>
                    <Ionicons
                        name="construct-outline"
                        size={48}
                        color={theme.palette.neutral[400]}
                    />
                    <Text
                        style={[
                            styles.placeholder,
                            { color: theme.palette.neutral[600] },
                        ]}
                    >
                        设置功能开发中
                    </Text>
                </View>
            </SafeAreaView>
        </>
    )
}

const styles = StyleSheet.create({
    root: { flex: 1 },
    topBar: {
        flexDirection: 'row',
        alignItems: 'center',
        justifyContent: 'space-between',
        paddingHorizontal: 8,
        paddingTop: 4,
    },
    backButton: {
        padding: 8,
        width: 40,
        alignItems: 'center',
    },
    topTitle: {
        fontSize: 16,
        fontWeight: '600',
        flex: 1,
        textAlign: 'center',
    },
    content: {
        flex: 1,
        alignItems: 'center',
        justifyContent: 'center',
        gap: 12,
    },
    placeholder: {
        fontSize: 15,
    },
})