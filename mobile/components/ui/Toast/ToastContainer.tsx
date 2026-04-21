import { View } from 'react-native'
import { useSafeAreaInsets } from 'react-native-safe-area-context'
import { useTheme } from '@/theme'
import { useToastStore } from './toastStore'
import { Toast } from './Toast'

export function ToastContainer() {
	const theme = useTheme()
	const insets = useSafeAreaInsets()
	const toasts = useToastStore((s) => s.toasts)
	const removeToast = useToastStore((s) => s.removeToast)

	return (
		<View
			style={{
				position: 'absolute',
				top: insets.top + theme.spacing[2],
				left: 0,
				right: 0,
				zIndex: 9999,
				gap: theme.spacing[2],
			}}
			pointerEvents="none"
		>
			{toasts.map((t) => (
				<Toast
					key={t.id}
					message={t.message}
					variant={t.variant}
					onDismiss={() => removeToast(t.id)}
				/>
			))}
		</View>
	)
}
