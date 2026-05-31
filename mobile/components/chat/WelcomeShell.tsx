/**
 * M7 · WelcomeShell。
 *
 * §3.2 决定：骨架固定（容器 + 居中排版 + 间距），content 接 ReactNode 由调用方
 * 完全自由组合（Mascot / 文案 / AB / 远程下发等变化都在调用方处理，骨架不动）。
 */
import type { ReactNode } from 'react'
import { StyleSheet, View } from 'react-native'

export type WelcomeShellProps = {
    content: ReactNode
}

export function WelcomeShell({ content }: WelcomeShellProps) {
    return <View style={styles.container}>{content}</View>
}

const styles = StyleSheet.create({
    container: {
        flex: 1,
        alignItems: 'center',
        justifyContent: 'center',
        paddingHorizontal: 24,
        gap: 16,
    },
})