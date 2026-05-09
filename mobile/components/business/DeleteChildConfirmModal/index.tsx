/**
 * DeleteChildConfirmModal — 父端确认删除某个孩子（M5 F5.5）。
 *
 * 严格按 https://www.notion.so/84a03fa603824d75bcbf88251b62cb0e F5 规范：
 *   - 标题「⚠️ 永久删除」+ body 顶部红色「危险操作」banner
 *   - 法规依据文案（个人信息保护法 / 未成年人网络保护条例 → 监护人删除权）
 *   - 输入框严格匹配 nickname（trim）才激活「确认删除」按钮
 *
 * 决策型 modal：confirm 瞬时关 modal + 把 mutation 委托给列表卡片的
 * 垃圾桶图标承载 loading（卡片本身在 mutation 成功后由 refetch 自然移除）。
 */
import { useEffect, useMemo, useState } from 'react'
import { View, Text, StyleSheet } from 'react-native'
import { Modal } from '@/components/ui/Modal'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { useTheme } from '@/theme'
import { NICKNAME_MAX } from '@/app/parent/children/new'

interface DeleteChildConfirmModalProps {
    visible: boolean
    onClose: () => void
    onConfirm: () => void
    childNickname: string
}

export function DeleteChildConfirmModal({
    visible,
    onClose,
    onConfirm,
    childNickname,
}: DeleteChildConfirmModalProps) {
    const theme = useTheme()
    const [input, setInput] = useState('')

    // modal 每次重新打开清空输入（避免上次残留）
    useEffect(() => {
        if (visible) {
            setInput('')
        }
    }, [visible])

    const isMatched = useMemo(
        () => input.trim() === childNickname,
        [input, childNickname],
    )

    return (
        <Modal
            visible={visible}
            onClose={onClose}
            title="⚠️ 永久删除"
            size="full"
            footer={
                <View style={styles.footerRow}>
                    <Button variant="ghost" size="md" onPress={onClose}>
                        取消
                    </Button>
                    <Button
                        variant="danger"
                        size="md"
                        disabled={!isMatched}
                        onPress={onConfirm}
                    >
                        确认删除
                    </Button>
                </View>
            }
        >
            <View style={styles.body}>
                {/* 危险操作 banner — 红底红字红边，最强视觉警示 */}
                <View
                    style={[
                        styles.dangerBanner,
                        {
                            backgroundColor: '#FEE2E2', // red-100，M14 视觉打磨可纳入 token
                            borderColor: theme.ui.error,
                        },
                    ]}
                >
                    <Text style={[styles.dangerBannerText, { color: theme.ui.error }]}>
                        ⚠️ 危险操作 · 此操作不可恢复
                    </Text>
                </View>

                {/* 法规依据 + 删除范围 */}
                <Text style={[styles.bodyText, { color: theme.palette.neutral[700] }]}>
                    根据《个人信息保护法》《未成年人网络保护条例》对监护人删除权的要求,我们将对
                    <Text style={styles.emphasis}>「{childNickname}」</Text>
                    的账号、聊天记录、报告、通知等所有个人信息进行
                    <Text style={styles.emphasis}>永久删除</Text>
                    ,
                    <Text style={[styles.emphasis, { color: theme.ui.error }]}>
                        不可恢复
                    </Text>
                    。
                </Text>

                {/* 输入确认指引 */}
                <Text style={[styles.guideText, { color: theme.palette.neutral[700] }]}>
                    请在下方输入孩子昵称
                    <Text style={styles.emphasis}>「{childNickname}」</Text>
                    以激活删除按钮。
                </Text>

                {/* 复用项目 Input 组件，与 login / new 页统一 */}
                <Input
                    value={input}
                    onChangeText={setInput}
                    placeholder={childNickname}
                    autoCapitalize="none"
                    autoCorrect={false}
                    maxLength={NICKNAME_MAX}
                />
            </View>
        </Modal>
    )
}

const styles = StyleSheet.create({
    body: {
        paddingVertical: 8,
        gap: 12,
    },
    dangerBanner: {
        paddingVertical: 10,
        paddingHorizontal: 12,
        borderRadius: 8,
        borderWidth: 1,
    },
    dangerBannerText: {
        fontSize: 14,
        fontWeight: '600',
    },
    bodyText: {
        fontSize: 14,
        lineHeight: 22,
    },
    emphasis: {
        fontWeight: '600',
    },
    guideText: {
        fontSize: 14,
        lineHeight: 22,
        marginTop: 4,
    },
    footerRow: {
        flexDirection: 'row',
        gap: 8,
        justifyContent: 'flex-end',
    },
})