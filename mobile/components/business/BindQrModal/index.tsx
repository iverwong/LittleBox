/**
 * BindQrModal — 父端为某个孩子申请绑定二维码 (M5 F5.3)。
 *
 * 父端打开后向后端申请 bind-token，渲染二维码供孩子端扫码。
 * 后端在孩子端 redeem 后将 token 状态置为 bound，
 * 前端 5 秒轮询发现后自动关闭并触发列表 refetch。
 *
 * F5.3b 落地：POST /bind-tokens 创建 + 5s 轮询 GET /bind-tokens/{token}/status。
 * 404 视为 token 过期（5 分钟 TTL），展示过期态 + 重新生成按钮。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
    View,
    Text,
    ActivityIndicator,
    Pressable,
    StyleSheet,
} from 'react-native'
import QRCode from 'react-native-qrcode-svg'
import { Modal } from '@/components/ui/Modal'
import { Button } from '@/components/ui/Button'
import { useTheme } from '@/theme'
import { api } from '@/services/api/client'
import { toast } from '@/components/ui/Toast'

interface BindQrModalProps {
    visible: boolean
    onClose: () => void
    childId: string
    childNickname: string
    /** 子端 redeem 成功后回调（通常用于 refetch 孩子列表） */
    onBindSuccess?: () => void
}

interface BindTokenResponse {
    bind_token: string
    expires_in_seconds: number
}

interface BindTokenStatusResponse {
    status: 'pending' | 'bound'
    child_user_id: string | null
    bound_at: string | null
}

type Phase = 'idle' | 'loading' | 'active' | 'expired' | 'error'

const POLL_INTERVAL_MS = 5000

export function BindQrModal({
    visible,
    onClose,
    childId,
    childNickname,
    onBindSuccess,
}: BindQrModalProps) {
    const theme = useTheme()
    const [token, setToken] = useState<string | null>(null)
    const [phase, setPhase] = useState<Phase>('idle')

    const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
    const cancelledRef = useRef(false)

    const stopPolling = useCallback(() => {
        if (pollTimerRef.current) {
            clearInterval(pollTimerRef.current)
            pollTimerRef.current = null
        }
    }, [])

    const requestNewToken = useCallback(async () => {
        cancelledRef.current = false
        stopPolling()
        setToken(null)
        setPhase('loading')

        const res = await api.post<BindTokenResponse>('/bind-tokens', {
            child_user_id: childId,
        })

        if (cancelledRef.current) return

        if (!res.ok) {
            setPhase('error')
            return
        }

        setToken(res.data.bind_token)
        setPhase('active')
    }, [childId, stopPolling])

    // 打开 modal → 申请 token；关闭 modal → 取消并清状态
    useEffect(() => {
        if (!visible) {
            cancelledRef.current = true
            stopPolling()
            setToken(null)
            setPhase('idle')
            return
        }
        requestNewToken()
        return () => {
            cancelledRef.current = true
            stopPolling()
        }
    }, [visible, requestNewToken, stopPolling])

    // active 阶段启动 5s 轮询
    useEffect(() => {
        if (phase !== 'active' || !token) return

        const tick = async () => {
            const res = await api.get<BindTokenStatusResponse>(
                `/bind-tokens/${token}/status`,
            )
            if (cancelledRef.current) return

            if (res.ok) {
                if (res.data.status === 'bound') {
                    stopPolling()
                    toast.show({
                        message: `${childNickname} 设备绑定成功`,
                        variant: 'success',
                        duration: 1800,
                    })
                    onBindSuccess?.()
                    onClose()
                }
                // pending → 继续轮询
                return
            }

            if (res.status === 404) {
                stopPolling()
                setPhase('expired')
                return
            }

            // 其他错误（5xx / 网络）：不停轮询，等下一轮重试，容忍 5s 内抖动
        }

        pollTimerRef.current = setInterval(tick, POLL_INTERVAL_MS)
        return () => stopPolling()
    }, [phase, token, childNickname, onBindSuccess, onClose, stopPolling])

    const renderQrArea = () => {
        if (phase === 'loading' || phase === 'idle') {
            return (
                <View style={styles.qrPlaceholder}>
                    <ActivityIndicator size="large" color={theme.palette.primary[500]} />
                </View>
            )
        }
        if (phase === 'error') {
            return (
                <View style={styles.qrPlaceholder}>
                    <Text style={[styles.errorText, { color: theme.report.redline }]}>
                        申请二维码失败
                    </Text>
                    <Pressable onPress={requestNewToken} style={styles.retryButton}>
                        <Text
                            style={[styles.retryText, { color: theme.palette.primary[600] }]}
                        >
                            重试
                        </Text>
                    </Pressable>
                </View>
            )
        }
        if (phase === 'expired') {
            return (
                <View style={styles.qrPlaceholder}>
                    <Text
                        style={[styles.errorText, { color: theme.palette.neutral[600] }]}
                    >
                        二维码已过期
                    </Text>
                    <Pressable onPress={requestNewToken} style={styles.retryButton}>
                        <Text
                            style={[styles.retryText, { color: theme.palette.primary[600] }]}
                        >
                            重新生成
                        </Text>
                    </Pressable>
                </View>
            )
        }
        // active
        return (
            <View style={styles.qrWrapper}>
                <QRCode
                    value={token!}
                    size={200}
                    backgroundColor="white"
                    color={theme.palette.secondary[700]}
                />
            </View>
        )
    }

    const hint =
        phase === 'active'
            ? '请用孩子的设备扫描上方二维码'
            : phase === 'expired'
                ? '二维码 5 分钟有效，请重新生成'
                : phase === 'error'
                    ? '请检查网络后重试'
                    : '正在申请二维码...'

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
                {renderQrArea()}
                <Text style={[styles.hint, { color: theme.palette.neutral[600] }]}>
                    {hint}
                </Text>
                {phase === 'active' && (
                    <Text style={[styles.subHint, { color: theme.palette.neutral[400] }]}>
                        扫码绑定后此页会自动关闭
                    </Text>
                )}
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
        gap: 8,
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
    errorText: {
        fontSize: 14,
        textAlign: 'center',
    },
    retryButton: {
        paddingVertical: 6,
        paddingHorizontal: 12,
    },
    retryText: {
        fontSize: 14,
        fontWeight: '500',
    },
})