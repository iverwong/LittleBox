/**
 * M7 · WelcomeShell 内容组件。
 *
 * 拉 /me/profile 取 nickname，按是否拿到展示问候文案。
 * 失败 / 404 / nickname null → 静默走 fallback，无 toast / 无重试（M5 既定纪律）。
 *
 * 迁自原 mobile/app/child/welcome.tsx，删原文件后取代之。
 */
import { useEffect, useState } from 'react'
import { StyleSheet, Text, View } from 'react-native'

import { Mascot } from '@/components/mascot/Mascot'
import { api } from '@/services/api/client'

/**
 * GET /me/profile 响应类型。
 * child_user_id 是 child 的 User.id（≠ ChildProfile.id PK）——见后端
 * app/schemas/children.py:ChildProfileOut 上个 commit 的命名调整。
 */
type ChildProfileOut = {
    child_user_id: string
    nickname: string | null
    gender: string
    birth_date: string
}

export function WelcomeContent() {
    const [nickname, setNickname] = useState<string | null>(null)

    useEffect(() => {
        let cancelled = false
        const fetchProfile = async () => {
            const result = await api.get<ChildProfileOut>('/me/profile')
            if (cancelled) return
            if (result.ok && result.data.nickname) {
                setNickname(result.data.nickname)
            }
            // 失败 / 404 / nickname 为 null：静默走 fallback 文案
        }
        void fetchProfile()
        return () => {
            cancelled = true
        }
    }, [])

    return (
        <View style={styles.center}>
            <Mascot />
            <Text style={styles.greeting}>
                {nickname ? `嗨 ${nickname}，我是小盒子！` : '嗨，我是小盒子！'}
            </Text>
        </View>
    )
}

const styles = StyleSheet.create({
    center: {
        flex: 1,
        justifyContent: 'center',
        alignItems: 'center',
        paddingHorizontal: 32,
    },
    greeting: {
        fontSize: 24,
        fontWeight: '600',
        color: '#2B2216',
        marginTop: 32,
        textAlign: 'center',
    },
})