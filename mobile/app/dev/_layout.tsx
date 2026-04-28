import { Stack } from 'expo-router'

// [M15-TEMP] Dev-only component gallery. Remove at M15.
export default function DevLayout() {
	return (
		<Stack
			screenOptions={{
				headerShown: true,
				title: '组件展厅',
			}}
		/>
	)
}
