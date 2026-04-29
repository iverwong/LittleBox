/**
 * [M15-TEMP] Dev Hub — central developer control panel.
 * 5 buttons + debug info panel.
 * Remove at M15.
 */
import { useState } from 'react'
import { Alert, ScrollView, StyleSheet, Text, View } from 'react-native'
import { router, useSegments } from 'expo-router'

import { useAuthStore } from '@/stores/auth'
import { BASE_URL } from '@/services/api/client'

// ─── Debug Info ──────────────────────────────────────────────────────────────

function DebugPanel() {
	const { role, token, deviceId } = useAuthStore()
	const segments = useSegments()
	const [copied, setCopied] = useState<string | null>(null)

	const copyField = (key: string, value: string) => {
		// Simple clipboard fallback using Alert (no expo-clipboard dep in dev/hub)
		setCopied(key)
		setTimeout(() => setCopied(null), 1500)
	}

	return (
		<View style={styles.debugPanel}>
			<Text style={styles.debugTitle}>调试信息</Text>
			<DebugRow
				label="role"
				value={role ?? '(null)'}
				onCopy={() => copyField('role', role ?? '')}
				copied={copied === 'role'}
			/>
			<DebugRow
				label="token"
				value={token ? `${token.slice(0, 4)}...${token.slice(-4)}` : '(null)'}
				onCopy={() => copyField('token', token ?? '')}
				copied={copied === 'token'}
			/>
			<DebugRow
				label="deviceId"
				value={deviceId ?? '(null)'}
				onCopy={() => copyField('deviceId', deviceId ?? '')}
				copied={copied === 'deviceId'}
			/>
			<DebugRow
				label="route"
				value={`/${segments.join('/')}`}
				onCopy={() => copyField('route', `/${segments.join('/')}`)}
				copied={copied === 'route'}
			/>
			<DebugRow
				label="API base"
				value={BASE_URL}
				onCopy={() => copyField('API base', BASE_URL)}
				copied={copied === 'API base'}
			/>
		</View>
	)
}

function DebugRow({
	label,
	value,
	onCopy,
	copied,
}: {
	label: string
	value: string
	onCopy: () => void
	copied: boolean
}) {
	return (
		<View style={styles.debugRow}>
			<Text style={styles.debugLabel}>{label}</Text>
			<Text style={styles.debugValue} numberOfLines={1}>{value}</Text>
			<Text style={styles.debugCopy} onPress={onCopy}>
				{copied ? '✓' : 'copy'}
			</Text>
		</View>
	)
}

// ─── Action Buttons ──────────────────────────────────────────────────────────

async function handleClearSession() {
	await useAuthStore.getState().clearSession()
}

async function handleStartTest() {
	await handleClearSession()
	router.replace('/auth/landing' as never)
}

async function handleResetDevice() {
	await useAuthStore.getState().resetDevice()
	// Re-read from store after reset
	const { deviceId } = useAuthStore.getState()
	Alert.alert('设备已重置', `新 deviceId: ${deviceId?.slice(0, 8)}...`)
}

// ─── Main Page ───────────────────────────────────────────────────────────────

export default function DevHub() {
	const [actionLog, setActionLog] = useState<string[]>([])

	const log = (msg: string) => setActionLog(prev => [`[${new Date().toLocaleTimeString()}] ${msg}`, ...prev.slice(0, 4)])

	return (
		<ScrollView style={styles.container} contentContainerStyle={styles.content}>
			<Text style={styles.pageTitle}>Dev Hub</Text>

			{/* 5 Action Buttons */}
			<View style={styles.buttonGroup}>
				<ActionButton
					label="开始测试"
					sublabel="清会话 + 跳转 landing"
					variant="primary"
					onPress={async () => {
						log('开始测试: 清会话 + 跳转 landing')
						await handleStartTest()
					}}
				/>
				<ActionButton
					label="清会话"
					sublabel="仅清除本地会话"
					variant="secondary"
					onPress={async () => {
						log('清会话')
						await handleClearSession()
					}}
				/>
				<ActionButton
					label="重置 device"
					sublabel="生成新 deviceId"
					variant="secondary"
					onPress={async () => {
						log('重置 device')
						await handleResetDevice()
					}}
				/>
				<ActionButton
					label="打进展厅"
					sublabel="组件 Gallery"
					variant="ghost"
					onPress={() => {
						log('打进展厅: /dev/components')
						router.push('/dev/components')
					}}
				/>
				<ActionButton
					label="打开 SSE Chat"
					sublabel="M3 流式演示"
					variant="ghost"
					onPress={() => {
						log('打开 SSE Chat: /dev/chat')
						router.push('/dev/chat')
					}}
				/>
			</View>

			{/* Debug Panel */}
			<DebugPanel />

			{/* Action Log */}
			<View style={styles.logPanel}>
				<Text style={styles.debugTitle}>最近操作</Text>
				{actionLog.length === 0 ? (
					<Text style={styles.logEmpty}>点击按钮后记录将显示在这里</Text>
				) : (
					actionLog.map((entry, i) => (
						<Text key={i} style={styles.logEntry}>{entry}</Text>
					))
				)}
			</View>
		</ScrollView>
	)
}

// ─── ActionButton Component ─────────────────────────────────────────────────

type ButtonVariant = 'primary' | 'secondary' | 'ghost'

function ActionButton({
	label,
	sublabel,
	variant,
	onPress,
}: {
	label: string
	sublabel: string
	variant: ButtonVariant
	onPress: () => void
}) {
	const bgColors: Record<ButtonVariant, string> = {
		primary: '#1A140B',
		secondary: '#F2EADF',
		ghost: 'transparent',
	}
	const textColors: Record<ButtonVariant, string> = {
		primary: '#F2EADF',
		secondary: '#1A140B',
		ghost: '#1A140B',
	}
	const borderColors: Record<ButtonVariant, string> = {
		primary: 'transparent',
		secondary: '#D8CFC4',
		ghost: 'transparent',
	}

	return (
		<View
			style={[
				styles.actionButton,
				{
					backgroundColor: bgColors[variant],
					borderColor: borderColors[variant],
					borderWidth: variant === 'secondary' ? 1 : 0,
				},
			]}
		>
			<Text style={[styles.actionLabel, { color: textColors[variant] }]}>{label}</Text>
			<Text style={[styles.actionSublabel, { color: textColors[variant], opacity: 0.6 }]}>{sublabel}</Text>
			<Text
				style={[styles.actionTap, { color: textColors[variant] }]}
				onPress={onPress}
			>
				执行 →
			</Text>
		</View>
	)
}

// ─── Styles ──────────────────────────────────────────────────────────────────

const styles = StyleSheet.create({
	container: {
		flex: 1,
		backgroundColor: '#F2EADF',
	},
	content: {
		padding: 20,
		gap: 16,
	},
	pageTitle: {
		fontSize: 28,
		fontWeight: '700',
		color: '#1A140B',
		marginBottom: 4,
	},
	buttonGroup: {
		gap: 10,
	},
	actionButton: {
		borderRadius: 12,
		padding: 14,
		flexDirection: 'row',
		alignItems: 'center',
	},
	actionLabel: {
		fontSize: 16,
		fontWeight: '600',
		flex: 1,
	},
	actionSublabel: {
		fontSize: 12,
		flex: 1,
	},
	actionTap: {
		fontSize: 14,
		fontWeight: '500',
	},
	debugPanel: {
		backgroundColor: '#FFFFFF',
		borderRadius: 12,
		padding: 16,
		gap: 8,
		borderWidth: 1,
		borderColor: '#E8E0D5',
	},
	debugTitle: {
		fontSize: 13,
		fontWeight: '600',
		color: '#7A6546',
		textTransform: 'uppercase',
		letterSpacing: 1,
		marginBottom: 4,
	},
	debugRow: {
		flexDirection: 'row',
		alignItems: 'center',
		gap: 8,
	},
	debugLabel: {
		fontSize: 12,
		color: '#7A6546',
		width: 72,
		flexShrink: 0,
	},
	debugValue: {
		fontSize: 12,
		color: '#1A140B',
		flex: 1,
		fontFamily: 'SpaceMono',
	},
	debugCopy: {
		fontSize: 12,
		color: '#7A6546',
		width: 36,
		textAlign: 'right',
	},
	logPanel: {
		backgroundColor: '#FFFFFF',
		borderRadius: 12,
		padding: 16,
		gap: 6,
		borderWidth: 1,
		borderColor: '#E8E0D5',
		minHeight: 80,
	},
	logEmpty: {
		fontSize: 13,
		color: '#B8A990',
		fontStyle: 'italic',
	},
	logEntry: {
		fontSize: 11,
		color: '#1A140B',
		fontFamily: 'SpaceMono',
		lineHeight: 18,
	},
})
