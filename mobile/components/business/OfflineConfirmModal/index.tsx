/**
 * OfflineConfirmModal — 父端确认下线某个孩子的设备 (M5 F5.4)。
 *
 * 决策型 modal：confirm 瞬时关 modal + 把 mutation 委托给列表按钮承载 loading。
 * 失败兜底：toast 报错 + 列表按钮 loading 退场；状态以服务端为准。
 *
 * 见 https://www.notion.so/4ec9256acb9546a1ad197ee74fa75420 §六「Modal 内 confirm 按钮不带 loading」+ 状态对齐三源规范。
 */
import { View, Text, StyleSheet } from 'react-native'
import { Modal } from '@/components/ui/Modal'
import { Button } from '@/components/ui/Button'
import { useTheme } from '@/theme'

interface OfflineConfirmModalProps {
    visible: boolean
    onClose: () => void
    onConfirm: () => void
    childNickname: string
}

export function OfflineConfirmModal({
    visible,
    onClose,
    onConfirm,
    childNickname,
}: OfflineConfirmModalProps) {
    const theme = useTheme()
    return (
        <Modal
            visible={visible}
            onClose={onClose}
            title="确认下线设备"
            size="md"
            footer={
                <View style={styles.footerRow}>
                    <Button variant="ghost" size="md" onPress={onClose}>
                        取消
                    </Button>
                    <Button variant="danger" size="md" onPress={onConfirm}>
                        下线
                    </Button>
                </View>
            }
        >
            <View style={styles.body}>
                <Text style={[styles.bodyText, { color: theme.palette.neutral[700] }]}>
                    下线后，
                    <Text style={{ fontWeight: '600' }} >{childNickname}</Text>
                    {' '}的设备会立即退出登录,需要重新扫码绑定才能继续使用。
                </Text>
            </View>
        </Modal >
    )
}

const styles = StyleSheet.create({
    body: {
        paddingVertical: 8,
    },
    bodyText: {
        fontSize: 15,
        lineHeight: 22,
    },
    footerRow: {
        flexDirection: 'row',
        gap: 8,
        justifyContent: 'flex-end',
    },
})