import { View } from 'react-native'
import { useSafeAreaInsets } from 'react-native-safe-area-context'
import Animated, {
	FadeInDown,
	FadeOut,
	LinearTransition,
} from 'react-native-reanimated'
import { useTheme } from '@/theme'
import { useToastStore } from './toastStore'
import { Toast } from './Toast'

export function ToastContainer() {
	const theme = useTheme()
	const insets = useSafeAreaInsets()
	const toasts = useToastStore((s) => s.toasts)

	return (
		<View
			style={{
				position: 'absolute',
				top: insets.top + theme.layout.headerHeight + theme.spacing[2] / 2,
				left: 0,
				right: 0,
				zIndex: 9999,
				gap: theme.spacing[2],
			}}
			pointerEvents="none"
		>
			{toasts.map((t) => (
				<Animated.View
					key={t.id}
					entering={FadeInDown.duration(200)}
					exiting={FadeOut.duration(200)}
					layout={LinearTransition.duration(200)}
				>
					<Toast
						message={t.message}
						variant={t.variant}
					/>
				</Animated.View>
			))}
		</View>
	)
}
