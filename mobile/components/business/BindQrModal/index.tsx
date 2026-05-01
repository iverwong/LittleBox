/**
 * BindQrModal — 父端为某个孩子申请绑定二维码 (M5 F5.3)。
 *
 * 父端打开后向后端申请 bind-token，渲染二维码供孩子端扫码。
 * 后端在孩子端 redeem 后将 token 状态置为 redeemed，
 * 前端轮询发现后自动关闭并触发列表 refetch。
 *
 * F5.3a: 视觉骨架 + mock token，不接 API。
 * F5.3b: 接 POST /bind-tokens + GET /bind-tokens/{token}/status 轮询 + 过期/失败态。
 */
import { useEffect, useState } from 'react'
import { View, Text, ActivityIndicator, StyleSheet } from 'react-native'
import QRCode from 'react-native-qrcode-svg'
import { Modal } from '@/components/ui/Modal'
import { Button } from '@/components/ui/Button'
import { useTheme } from '@/theme'

interface BindQrModalProps {
    visible: boolean
    onClose: () => void
    childId: string
    childNickname: string
}

export function BindQrModal({
    visible,
    onClose,
    childId,
    childNickname,
}: BindQrModalProps) {
    const theme = useTheme()
    const [token, setToken] = useState<string | null>(null)
    const [loading, setLoading] = useState(false)

    // F5.3a: mock token，不调 API。F5.3b 替换为 POST /bind-tokens
    useEffect(() => {
        if (!visible) {
            setToken(null)
            return
        }
        setLoading(true)
        const t = setTimeout(() => {
            setToken(`DEMO-TOKEN-${childId}-${Date.now()}`)
            setLoading(false)
        }, 500)
        return () => clearTimeout(t)
    }, [visible, childId])

    return (
        <Modal
            visible={visible}
            onClose={onClose}
            title={`为 ${childNickname} 绑定设备`}
            size="md"
            footer={
                <Button variant="ghost" size="md" onPress={onClose}>
                    取消
                </Button>
            }
        >
            <View style={styles.body}>
                {loading || !token ? (
                    <View style={styles.qrPlaceholder}>
                        <ActivityIndicator
                            size="large"
                            color={theme.palette.primary[500]}
                        />
                    </View>
                ) : (
                    <View style={styles.qrWrapper}>
                        <QRCode
                            value={token}
                            size={200}
                            backgroundColor="white"
                            color={theme.palette.secondary[700]}
                        />
                    </View>
                )}
                <Text style={[styles.hint, { color: theme.palette.neutral[600] }]}>
                    请用孩子的设备扫描上方二维码
                </Text>
                <Text style={[styles.subHint, { color: theme.palette.neutral[400] }]}>
                    扫码绑定后此页会自动关闭
                </Text>
            </View>
        </Modal>
    )
}

const styles = StyleSheet.create({
    body: {
        alignItems: 'center',
        paddingVertical: 16,
        gap: 12,
    },
    qrPlaceholder: {
        width: 200,
        height: 200,
        alignItems: 'center',
        justifyContent: 'center',
    },
    qrWrapper: {
        padding: 16,
        backgroundColor: 'white',
        borderRadius: 12,
    },
    hint: {
        fontSize: 14,
        textAlign: 'center',
        marginTop: 8,
    },
    subHint: {
        fontSize: 12,
        textAlign: 'center',
    },
})