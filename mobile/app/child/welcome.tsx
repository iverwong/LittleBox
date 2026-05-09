import { Stack } from 'expo-router'
import { useEffect, useState } from 'react'
import { StyleSheet, Text, View } from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'

import { Mascot } from '@/components/mascot/Mascot'
import { api } from '@/services/api/client'

type ChildProfileOut = {
    id: string
    nickname: string | null
    gender: string
    birth_date: string
}

export default function WelcomeScreen() {
    const [nickname, setNickname] = useState<string | null>(null)

    useEffect(() => {
        let cancelled = false
        const fetchProfile = async () => {
            const result = await api.get<ChildProfileOut>('/me/profile')
            if (cancelled) return
            if (result.ok && result.data.nickname) {
                setNickname(result.data.nickname)
            }
            // 失败 / 404 / nickname 为 null：静默走 fallback 文案，无 toast、无重试
        }
        fetchProfile()
        return () => {
            cancelled = true
        }
    }, [])

    return (
        <SafeAreaView style={styles.container} edges={['top', 'bottom']}>
            <Stack.Screen options={{ headerShown: false }} />
            <View style={styles.center}>
                <Mascot size="xl" />
                <Text style={styles.greeting}>
                    {nickname ? `嗨 ${nickname}，我是小盒子！` : '嗨，我是小盒子！'}
                </Text>
            </View>
            <Text style={styles.footnote}>聊天能力将在后续版本开放</Text>
        </SafeAreaView>
    )
}

const styles = StyleSheet.create({
    container: {
        flex: 1,
        backgroundColor: '#F2EADF',
    },
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
    footnote: {
        fontSize: 14,
        color: '#998260',
        textAlign: 'center',
        paddingBottom: 16,
    },
})