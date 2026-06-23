/**
 * UnsavedChangesModal — 未保存退出确认（普通确认范式，非危险操作）。
 */
import { StyleSheet, Text } from 'react-native'
import { Modal } from '@/components/ui/Modal'
import { Button } from '@/components/ui/Button'
import { useTheme } from '@/theme'

interface UnsavedChangesModalProps {
    visible: boolean
    onCancel: () => void
    onConfirmLeave: () => void
}

export function UnsavedChangesModal({ visible, onCancel, onConfirmLeave }: UnsavedChangesModalProps) {
    const theme = useTheme()
    return (
        <Modal
            visible={visible}
            onClose={onCancel}
            title="修改还没保存哦"
            footer={
                <>
                    <Button variant="ghost" onPress={onConfirmLeave}>
                        直接退出
                    </Button>
                    <Button variant="primary" onPress={onCancel}>
                        继续编辑
                    </Button>
                </>
            }
        >
            <Text style={[styles.body, { color: theme.palette.neutral[600] }]}>
                你刚调整的内容还没保存。点击右上角的「保存」进行保留；直接退出，以放弃此次修改。
            </Text>
        </Modal>
    )
}

const styles = StyleSheet.create({
    body: { fontSize: 15, lineHeight: 22 },
})