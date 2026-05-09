import { Stack } from 'expo-router'

// [M15-TEMP] Dev-only production guard. Remove at M15.
if (!__DEV__) {
	throw new Error('dev routes are dev-only')
}

// [M15-TEMP] Dev-only component gallery. Remove at M15.
export default function DevLayout() {
	return (
		<Stack
			screenOptions={{
				headerShown: true,
				title: 'Dev Tools',
			}}
		/>
	)
}
