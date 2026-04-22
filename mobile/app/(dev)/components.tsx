// [M15-TEMP] Component gallery for visual QA against HTML prototype v0.5.
// Verify all components match design tokens; surface token diff is expected.
import { useState, useCallback } from 'react'
import { ScrollView, View, Text, Pressable } from 'react-native'
import { router } from 'expo-router'
import { Feather } from '@expo/vector-icons'
import { useTheme } from '@/theme'
import { useSafeAreaInsets } from 'react-native-safe-area-context'
import {
	Button,
	Input,
	Card,
	Avatar,
	Loading,
	EmptyState,
	ListItem,
	Modal,
	toast,
	ToastContainer,
} from '@/components/ui'
import { ScreenContainer, Header, ChatBubble } from '@/components/layout'
import { Mascot } from '@/components/mascot'
import type { ToastVariant, ButtonVariant, ButtonSize, ModalSize, AvatarSize, CardVariant } from '@/components/ui'
import type { MascotState, MascotSize } from '@/components/mascot'
import type { BubbleRole } from '@/components/layout/ChatBubble'

// ─── Layout helpers ──────────────────────────────────────────────────────────

const SectionHeader = ({ title }: { title: string }) => (
	<Text style={{ fontSize: 11, fontWeight: '600', color: '#888', letterSpacing: 1, marginTop: 24, marginBottom: 8, paddingHorizontal: 16 }}>{title.toUpperCase()}</Text>
)

const Chip = ({ color, label, textColor }: { color: string; label: string; textColor?: string }) => (
	<View style={{ width: 64, alignItems: 'center' }}>
		<View style={{ width: 44, height: 44, borderRadius: 8, backgroundColor: color }} />
		<Text style={{ fontSize: 9, color: textColor ?? '#555', marginTop: 4, textAlign: 'center' }}>{label}</Text>
	</View>
)

// ─── Colors ──────────────────────────────────────────────────────────────────

function ColorsSection() {
	const theme = useTheme()
	const p = theme.palette
	const ui = theme.ui
	const report = theme.report
	const tags = theme.tags

	return (
		<>
			<SectionHeader title="Colors / Palette / Primary" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				{(['50','100','200','300','400','500','600','700','800','900'] as const).map(k => (
					<Chip key={k} color={p.primary[k]} label={k} />
				))}
			</View>

			<SectionHeader title="Colors / Palette / Secondary" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				{(['50','100','200','300','400','500','600','700','800','900'] as const).map(k => (
					<Chip key={k} color={p.secondary[k]} label={k} />
				))}
			</View>

			<SectionHeader title="Colors / Palette / Neutral" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				{(['50','100','200','300','400','500','600','700','800','900'] as const).map(k => (
					<Chip key={k} color={p.neutral[k]} label={k} />
				))}
			</View>

			<SectionHeader title="Colors / UI" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				<Chip color={ui.error} label="error" />
				<Chip color={ui.warning} label="warning" />
				<Chip color={ui.success} label="success" />
				<Chip color={ui.info} label="info" />
			</View>

			<SectionHeader title="Colors / Report" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				<Chip color={report.crisis} label="crisis" />
				<Chip color={report.redline} label="redline" />
				<Chip color={report.guidance} label="guidance" />
				<Chip color={report.safe} label="safe" />
			</View>

			<SectionHeader title="Colors / Tags" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				{Object.entries(tags).map(([k, v]) => (
					<Chip key={k} color={v} label={k} />
				))}
			</View>

			<SectionHeader title="Colors / Surface" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				<Chip color={theme.surface.paper} label="paper" textColor="#000" />
			</View>
		</>
	)
}

// ─── Typography ───────────────────────────────────────────────────────────────

function TypographySection() {
	const theme = useTheme()
	const sizes = ['xs','sm','base','md','lg','xl','2xl','3xl'] as const
	const weights = ['regular','medium','semibold','bold'] as const

	return (
		<>
			<SectionHeader title="Typography" />
			<View style={{ paddingHorizontal: 16, gap: 8 }}>
				{sizes.map(size => (
					<View key={size} style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
						<Text style={{ width: 40, fontSize: 11, color: '#888' }}>{size}</Text>
						{weights.map(w => (
							<Text
								key={w}
								style={{
									fontSize: theme.typography.fontSize[size],
									fontWeight: theme.typography.fontWeight[w],
									color: '#222',
								}}
							>
								{size}/{w}
							</Text>
						))}
					</View>
				))}
			</View>
		</>
	)
}

// ─── Spacing / Radius / Shadow ───────────────────────────────────────────────

function SpacingSection() {
	const theme = useTheme()
	const entries = Object.entries(theme.spacing) as [string, number][]

	return (
		<>
			<SectionHeader title="Spacing" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				{entries.map(([k, v]) => (
					<View key={k} style={{ alignItems: 'center', minWidth: 48 }}>
						<View style={{ width: v, height: 20, backgroundColor: '#A67148', borderRadius: 4 }} />
						<Text style={{ fontSize: 10, color: '#888', marginTop: 2 }}>{k}={v}</Text>
					</View>
				))}
			</View>
		</>
	)
}

function RadiusSection() {
	const theme = useTheme()
	const entries = Object.entries(theme.radius) as [string, number][]

	return (
		<>
			<SectionHeader title="Radius" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				{entries.map(([k, v]) => (
					<View key={k} style={{ alignItems: 'center', minWidth: 56 }}>
						<View
							style={{
								width: 40,
								height: 40,
								backgroundColor: '#A67148',
								borderRadius: v,
							}}
						/>
						<Text style={{ fontSize: 10, color: '#888', marginTop: 4 }}>{k}</Text>
					</View>
				))}
			</View>
		</>
	)
}

function ShadowSection() {
	const theme = useTheme()
	const variants = ['sm','md','lg'] as const

	return (
		<>
			<SectionHeader title="Shadow" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				{variants.map(v => (
					<View key={v} style={{ alignItems: 'center' }}>
						<View
							style={{
								width: 60,
								height: 40,
								backgroundColor: '#FAFAFA',
								borderRadius: 8,
								...theme.shadow[v],
							}}
						/>
						<Text style={{ fontSize: 10, color: '#888', marginTop: 4 }}>shadow.{v}</Text>
					</View>
				))}
			</View>
		</>
	)
}

// ─── Button ───────────────────────────────────────────────────────────────────

function ButtonSection() {
	const variants: ButtonVariant[] = ['primary','secondary','ghost','danger']
	const sizes: ButtonSize[] = ['sm','md','lg']

	return (
		<>
			<SectionHeader title="Button (4 variant × 3 size)" />
			<View style={{ paddingHorizontal: 16, gap: 12 }}>
				{variants.map(v => (
					<View key={v} style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
						<Text style={{ width: 60, fontSize: 11, color: '#888', alignSelf: 'center' }}>{v}</Text>
						{sizes.map(s => (
							<Button key={s} variant={v} size={s}>
								{BtnLabel(s)}
							</Button>
						))}
					</View>
				))}
				<SectionHeader title="Button Loading" />
				<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
					<Button variant="primary" size="md" loading>加载中</Button>
					<Button variant="secondary" size="md" loading>加载中</Button>
					<Button variant="danger" size="md" loading />
				</View>
				<SectionHeader title="Button Disabled" />
				<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
					<Button variant="primary" size="md" disabled>禁用</Button>
					<Button variant="ghost" size="md" disabled>禁用</Button>
				</View>
			</View>
		</>
	)
}

function BtnLabel(s: ButtonSize) {
	return { sm: 'Small', md: 'Medium', lg: 'Large' }[s]
}

// ─── Input ────────────────────────────────────────────────────────────────────

function InputSection() {
	const [valDefault, setValDefault] = useState('')
	const [valFocused, setValFocused] = useState('聚焦态内容')
	const [valError, setValError] = useState('')
	const [errMsg, setErrMsg] = useState('')

	return (
		<>
			<SectionHeader title="Input" />
			<View style={{ paddingHorizontal: 16, gap: 12 }}>
				<Input
					value={valDefault}
					onChangeText={t => { setValDefault(t); setErrMsg('') }}
					placeholder="默认状态"
					label="默认"
					size="md"
				/>
				<Input
					value={valFocused}
					onChangeText={setValFocused}
					placeholder="Focused"
					label="聚焦"
					size="md"
				/>
				<Input
					value={valError || 'invalid@example'}
					onChangeText={t => { setValError(t); setErrMsg(t.length > 10 ? '输入错误' : '') }}
					placeholder="Error"
					label="错误态"
					error={errMsg || undefined}
					size="md"
				/>
				<Input
					value="禁用内容"
					onChangeText={() => {}}
					placeholder="Disabled"
					label="禁用"
					disabled
					size="md"
				/>
			</View>
		</>
	)
}

// ─── Card ─────────────────────────────────────────────────────────────────────

function CardSection() {
	const variants: CardVariant[] = ['elevated','outlined','filled','accentSecondary']

	return (
		<>
			<SectionHeader title="Card (4 variant)" />
			<View style={{ paddingHorizontal: 16, gap: 12 }}>
				{variants.map(v => (
					<Card key={v} variant={v} padding={4}>
						<Text style={{ fontSize: 14, color: '#333' }}>Card variant={v}</Text>
						<Text style={{ fontSize: 12, color: '#777', marginTop: 4 }}>内边距 padding=4</Text>
					</Card>
				))}
			</View>
		</>
	)
}

// ─── Avatar ───────────────────────────────────────────────────────────────────

function AvatarSection() {
	const sizes: AvatarSize[] = ['sm','md','lg','xl']
	const names = ['李小白', '陈明', '王芳', '刘洋']

	return (
		<>
			<SectionHeader title="Avatar (4 sizes)" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				{sizes.map((s, i) => (
					<Avatar key={s} name={names[i]} size={s} />
				))}
			</View>
		</>
	)
}

// ─── Loading ─────────────────────────────────────────────────────────────────

function LoadingSection() {
	const sizes: ('sm' | 'md' | 'lg')[] = ['sm','md','lg']

	return (
		<>
			<SectionHeader title="Loading (3 sizes)" />
			<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
				{sizes.map(s => (
					<View key={s} style={{ alignItems: 'center' }}>
						<Loading size={s} />
						<Text style={{ fontSize: 10, color: '#888', marginTop: 4 }}>size={s}</Text>
					</View>
				))}
			</View>
		</>
	)
}

// ─── ListItem ─────────────────────────────────────────────────────────────────

function ListItemSection() {
	return (
		<>
			<SectionHeader title="ListItem" />
			<View style={{ paddingHorizontal: 16 }}>
				<ListItem
					leading={<Avatar name="李" size="sm" />}
					title="李小白"
					subtitle="最后消息：今天过得怎么样？"
					trailing={<Text style={{ fontSize: 12, color: '#999' }}>刚刚</Text>}
					divider
				/>
				<ListItem
					leading={<Avatar name="陈" size="sm" backgroundColor="#7A9180" />}
					title="陈明"
					subtitle="最后消息：好的，收到！"
					trailing={<Text style={{ fontSize: 12, color: '#999' }}>5分钟前</Text>}
					divider
				/>
				<ListItem
					leading={<Avatar name="+" size="sm" backgroundColor="#D89155" />}
					title="添加孩子"
					trailing={<Feather name="chevron-right" size={18} color="#999" />}
					onPress={() => {}}
				/>
			</View>
		</>
	)
}

// ─── EmptyState ───────────────────────────────────────────────────────────────

function EmptyStateSection() {
	return (
		<>
			<SectionHeader title="EmptyState" />
			<View style={{ paddingHorizontal: 16 }}>
				<EmptyState
					icon="inbox"
					title="暂无消息"
					description="开始一段对话吧，孩子和家长可以在这里交流。"
				/>
			</View>
		</>
	)
}

// ─── Modal ────────────────────────────────────────────────────────────────────

function ModalSection() {
	const [visible, setVisible] = useState(false)
	const [size, setSize] = useState<ModalSize>('md')

	return (
		<>
			<SectionHeader title="Modal (trigger × size selector)" />
			<View style={{ paddingHorizontal: 16, gap: 8 }}>
				<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
					{(['sm','md','lg','full'] as ModalSize[]).map(s => (
						<Button key={s} variant="secondary" size="sm" onPress={() => { setSize(s); setVisible(true) }}>
							{s}
						</Button>
					))}
				</View>
				<Modal
					visible={visible}
					title="组件标题"
					size={size}
					onClose={() => setVisible(false)}
					footer={
						<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
							<Button variant="ghost" size="md" onPress={() => setVisible(false)}>取消</Button>
							<Button variant="primary" size="md" onPress={() => setVisible(false)}>确认</Button>
						</View>
					}
				>
					<Text style={{ fontSize: 14, color: '#555', lineHeight: 22 }}>
						这是 Modal 内容区域，可以放置任意内容。这里演示了不同 size 下的面板宽度效果。
					</Text>
				</Modal>
			</View>
		</>
	)
}

// ─── Toast ─────────────────────────────────────────────────────────────────────

const TOAST_VARIANTS: ToastVariant[] = ['info','success','warning','error']

function ToastSection() {
	return (
		<>
			<SectionHeader title="Toast (4 variant triggers)" />
			<View style={{ paddingHorizontal: 16, gap: 8 }}>
				<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
					{TOAST_VARIANTS.map(v => (
						<Button
							key={v}
							variant={v === 'warning' ? 'secondary' : v === 'error' ? 'danger' : 'primary'}
							size="sm"
							onPress={() => toast.show({ message: ToastMsg(v), variant: v })}
						>
							{v}
						</Button>
					))}
				</View>
			</View>
		</>
	)
}

function ToastMsg(v: ToastVariant): string {
	const msgs: Record<ToastVariant, string> = {
		info: '这是一条普通提示',
		success: '操作成功！',
		warning: '请注意这个警告',
		error: '出错了，请重试',
	}
	return msgs[v]
}

// ─── ScreenContainer ───────────────────────────────────────────────────────────

function ScreenContainerSection() {
	const bgOptions: ('primary'|'secondary'|'neutral')[] = ['primary','secondary','neutral']

	return (
		<>
			<SectionHeader title="ScreenContainer (3 backgrounds)" />
			<View style={{ paddingHorizontal: 16, gap: 8 }}>
				{bgOptions.map(bg => (
					<ScreenContainer key={bg} background={bg} style={{ height: 80 }}>
						<Text style={{ fontSize: 13, color: '#fff' }}>background={bg}</Text>
					</ScreenContainer>
				))}
			</View>
		</>
	)
}

// ─── Header ───────────────────────────────────────────────────────────────────

function HeaderSection() {
	return (
		<>
			<SectionHeader title="Header" />
			<View style={{ paddingHorizontal: 0 }}>
				<Header
					title="会话详情"
					subtitle="与 李小白 的对话"
					leading={<Pressable onPress={() => router.back()}><Feather name="arrow-left" size={22} color="#333" /></Pressable>}
					trailing={
						<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 16, paddingHorizontal: 16 }}>
							<Pressable><Feather name="more-vertical" size={22} color="#333" /></Pressable>
						</View>
					}
				/>
			</View>
		</>
	)
}

// ─── ChatBubble ────────────────────────────────────────────────────────────────

function ChatBubbleSection() {
	const roles: BubbleRole[] = ['ai','user']

	return (
		<>
			<SectionHeader title="ChatBubble (ai × user)" />
			<View style={{ paddingHorizontal: 16, gap: 8 }}>
				{roles.map(r => (
					<ChatBubble key={r} role={r}>
						{r === 'ai' ? 'AI 回复：今天过得怎么样？有什么有趣的事吗？' : '孩子回复：今天学校有运动会！'}
					</ChatBubble>
				))}
			</View>
		</>
	)
}

// ─── Mascot ────────────────────────────────────────────────────────────────────

const MASCOT_STATES: MascotState[] = ['enter','idle','listen','thinking','narrating','done']
const MASCOT_SIZES: MascotSize[] = ['sm','md','lg','xl']

function MascotSection() {
	const [finishCount, setFinishCount] = useState(0)
	const [currentState, setCurrentState] = useState<MascotState>('idle')

	const handleFinish = useCallback(() => {
		setFinishCount(c => c + 1)
	}, [])

	return (
		<>
			<SectionHeader title="Mascot (6 state × 4 size) + onFinish counter" />
			<View style={{ paddingHorizontal: 16, gap: 16 }}>
				{/* State selector */}
				<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 16 }}>
					{MASCOT_STATES.map(s => (
						<Button
							key={s}
							variant={currentState === s ? 'primary' : 'secondary'}
							size="sm"
							onPress={() => setCurrentState(s)}
						>
							{s}
						</Button>
					))}
				</View>
				{/* Size matrix */}
				<View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 12, justifyContent: 'center' }}>
					{MASCOT_SIZES.map(size => (
						<View key={size} style={{ alignItems: 'center' }}>
							<Mascot
								state={currentState}
								size={size}
								onFinish={handleFinish}
							/>
							<Text style={{ fontSize: 10, color: '#888', marginTop: 4 }}>{size}</Text>
						</View>
					))}
				</View>
				{/* onFinish counter */}
				<View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
					<Text style={{ fontSize: 13, color: '#555' }}>一次性态 onFinish 触发次数：</Text>
					<Text style={{ fontSize: 20, fontWeight: '700', color: '#A67148' }}>{finishCount}</Text>
					<Button variant="ghost" size="sm" onPress={() => setFinishCount(0)}>重置</Button>
				</View>
				<Text style={{ fontSize: 11, color: '#999' }}>
					说明：触发 enter / done，计数器 +1；idle / listen / thinking / narrating 不触发回调。
				</Text>
			</View>
		</>
	)
}

// ─── Main Gallery Page ────────────────────────────────────────────────────────

export default function ComponentGallery() {
	const insets = useSafeAreaInsets()

	return (
		<View style={{ flex: 1, backgroundColor: '#F2EADF' }}>
			<ScrollView
				style={{ flex: 1 }}
				contentContainerStyle={{ paddingTop: insets.top + 16, paddingBottom: insets.bottom + 24 }}
			>
				{/* Nav back */}
				<View style={{ paddingHorizontal: 16, marginBottom: 8 }}>
					<Button variant="ghost" size="sm" leftIcon="arrow-left" onPress={() => router.back()}>
						返回 Dev-Chat
					</Button>
				</View>

				<SectionHeader title=" Foundations" />
				<ColorsSection />
				<TypographySection />
				<SpacingSection />
				<RadiusSection />
				<ShadowSection />

				<SectionHeader title="UI Components" />
				<ButtonSection />
				<InputSection />
				<CardSection />
				<AvatarSection />
				<LoadingSection />
				<ListItemSection />
				<EmptyStateSection />
				<ModalSection />
				<ToastSection />

				<SectionHeader title="Layout Components" />
				<ScreenContainerSection />
				<HeaderSection />
				<ChatBubbleSection />

				<SectionHeader title="Mascot" />
				<MascotSection />
			</ScrollView>

			{/* ToastContainer must be inside the page to use useSafeAreaInsets */}
			<ToastContainer />
		</View>
	)
}
