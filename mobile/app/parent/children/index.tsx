/**
 * parent/children/index.tsx — children list page (M5 F5.1).
 *
 * F5.1 重构：
 * - 去 Stack.Screen header；Mascot 锚定页面身份
 * - 空态：Mascot lg + 提示「点击下方按钮添加您的孩子」+ 主按钮「添加第一个孩子」
 * - 列表态：Mascot md + 提示「这里是你的孩子们」+ 列表 + 列表下方主按钮「添加孩子（X/3）」
 * - 满 3 个：按钮真 disabled，无 toast（文案 X/3 自带语义,F4.6 偏差反修)
 * - 失败态：保留 EmptyState 重试卡片(首次加载失败)
 * - F4.7 ActivityIndicator 首次加载方案保留;SkeletonCard 已废弃,清理
 *
 * F5 后续小步会接：
 * - ChildCard onPress    → /parent/children/[id]/settings (F5.6)
 * - handlePrimaryAction  → BindQrModal / OfflineConfirmModal (F5.3 / F5.4)
 * - handleTrashPress     → DeleteChildConfirmModal (F5.5)
 */
import { useCallback, useRef, useState } from 'react'
import {
  View,
  Text,
  FlatList,
  RefreshControl,
  Pressable,
  StyleSheet,
  ActivityIndicator,
  ScrollView,
} from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'
import { Stack, useRouter } from 'expo-router'
import { Ionicons } from '@expo/vector-icons'
import { useFocusEffect } from '@react-navigation/native'

import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { EmptyState } from '@/components/ui/EmptyState'
import { ListItem } from '@/components/ui/ListItem'
import { toast } from '@/components/ui/Toast'
import { useTheme } from '@/theme'
import { api } from '@/services/api/client'
import { GenderAvatar } from '@/components/business/GenderAvatar'
import { birthDateToAge } from '@/lib/birthDateUtils'
import { Mascot } from '@/components/mascot/Mascot'
import { BindQrModal } from '@/components/business/BindQrModal'
import { OfflineConfirmModal } from '@/components/business/OfflineConfirmModal'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Gender = 'male' | 'female' | 'unknown'

interface ChildSummary {
  id: string
  nickname: string
  birth_date: string // "YYYY-MM-DD"
  gender: Gender
  is_bound: boolean
}

// ---------------------------------------------------------------------------
// Child card
// ---------------------------------------------------------------------------

interface ChildCardProps {
  child: ChildSummary
  onChildStateChanged?: () => void
}

function ChildCard({ child, onChildStateChanged: onChildBound }: ChildCardProps) {
  const theme = useTheme()
  const age = birthDateToAge(child.birth_date)
  const primaryActionLabel = child.is_bound ? '下线设备' : '绑定设备'
  const dangerColor = theme.ui.error

  const [bindModalVisible, setBindModalVisible] = useState(false)
  const [offlineModalVisible, setOfflineModalVisible] = useState(false)
  const [revoking, setRevoking] = useState(false)

  const handleListItemPress = useCallback(() => {
    toast.show({ message: '孩子设置页 F5 上线', variant: 'info', duration: 1500 })
  }, [])

  const handlePrimaryAction = useCallback(() => {
    if (child.is_bound) {
      setOfflineModalVisible(true)
      return
    }
    setBindModalVisible(true)
  }, [child.is_bound])

  const handleConfirmOffline = useCallback(async () => {
    setOfflineModalVisible(false)
    setRevoking(true)
    const res = await api.post(`/children/${child.id}/revoke-tokens`, {})
    setRevoking(false)
    if (!res.ok) {
      toast.show({ message: '下线失败,请稍后重试', variant: 'error', duration: 3000 })
      return
    }
    toast.show({
      message: `${child.nickname} 的设备已下线`,
      variant: 'success',
      duration: 1500,
    })
    onChildBound?.()
  }, [child.id, child.nickname, onChildBound])

  const handleTrashPress = useCallback(() => {
    toast.show({ message: '删除功能开发中', variant: 'info', duration: 1500 })
  }, [])

  return (
    <>
      <Card variant="outlined" padding={0} style={styles.cardSpacing}>
        <ListItem
          leading={<GenderAvatar gender={child.gender} size={48} />}
          title={`${child.nickname} · ${age}岁`}
          trailing={
            <Ionicons
              name="chevron-forward"
              size={20}
              color={theme.palette.secondary[400]}
            />
          }
          onPress={handleListItemPress}
          divider
        />
        <View style={styles.actionRow}>
          <Button
            variant={child.is_bound ? 'danger' : 'primary'}
            size="md"
            style={styles.flex1}
            loading={revoking}
            onPress={handlePrimaryAction}
          >
            {primaryActionLabel}
          </Button>
          <Pressable
            onPress={handleTrashPress}
            hitSlop={8}
            style={styles.trashIconButton}
          >
            <Ionicons name="trash-outline" size={20} color={dangerColor} />
          </Pressable>
        </View>
      </Card>
      <BindQrModal
        visible={bindModalVisible}
        onClose={() => setBindModalVisible(false)}
        childId={child.id}
        childNickname={child.nickname}
        onBindSuccess={onChildBound}
      />
      <OfflineConfirmModal
        visible={offlineModalVisible}
        onClose={() => setOfflineModalVisible(false)}
        onConfirm={handleConfirmOffline}
        childNickname={child.nickname}
      />
    </>
  )
}

// ---------------------------------------------------------------------------
// Main screen
// ---------------------------------------------------------------------------

const QUOTA = 3

export default function ChildrenIndexScreen() {
  const theme = useTheme()
  const router = useRouter()

  const [children, setChildren] = useState<ChildSummary[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const isInitialLoadRef = useRef(true)

  const fetchChildren = useCallback(async () => {
    if (abortRef.current) abortRef.current.abort()
    const controller = new AbortController()
    abortRef.current = controller

    const res = await api.get<{ children: ChildSummary[] }>('/children')
    if (!res.ok) {
      if (!controller.signal.aborted) {
        if (isInitialLoadRef.current) {
          setError(true)
          setLoading(false)
        } else {
          toast.show({ message: '刷新失败', variant: 'error', duration: 2000 })
        }
      }
      return
    }
    if (!controller.signal.aborted) {
      setChildren(res.data.children)
      setLoading(false)
      setError(false)
      isInitialLoadRef.current = false
    }
  }, [])

  useFocusEffect(
    useCallback(() => {
      if (isInitialLoadRef.current) {
        setLoading(true)
        setError(false)
      }
      fetchChildren()
      return () => {
        abortRef.current?.abort()
      }
    }, [fetchChildren]),
  )

  const handleRefresh = useCallback(async () => {
    setRefreshing(true)
    await fetchChildren()
    setRefreshing(false)
  }, [fetchChildren])

  const handleAddPress = useCallback(() => {
    if ((children?.length ?? 0) >= QUOTA) return
      ; (router.push as (href: string) => void)('/parent/children/new')
  }, [children, router])

  const renderItem = useCallback(
    ({ item }: { item: ChildSummary }) => <ChildCard child={item} onChildStateChanged={fetchChildren} />,
    [fetchChildren],
  )

  const childCount = children?.length ?? 0
  const atQuota = childCount >= QUOTA

  return (
    <>
      <Stack.Screen options={{ headerShown: false }} />

      <SafeAreaView
        style={[styles.container, { backgroundColor: theme.surface.paper }]}
        edges={['top', 'left', 'right']}
      >
        {loading && !refreshing ? (
          <View style={styles.centerBox}>
            <ActivityIndicator size="large" color={theme.palette.primary[500]} />
          </View>
        ) : error ? (
          <ScrollView
            contentContainerStyle={styles.centerBox}
            refreshControl={
              <RefreshControl refreshing={refreshing} onRefresh={handleRefresh} />
            }
          >
            <EmptyState
              icon="alert-circle"
              title="加载失败"
              description="请检查网络后重试"
              action={
                <Button variant="ghost" size="md" onPress={fetchChildren}>
                  点击重试
                </Button>
              }
            />
          </ScrollView>
        ) : childCount === 0 ? (
          <ScrollView
            contentContainerStyle={styles.emptyContent}
            refreshControl={
              <RefreshControl refreshing={refreshing} onRefresh={handleRefresh} />
            }
          >
            <Mascot size="lg" />
            <Text style={[styles.subtitle, { color: theme.palette.neutral[500] }]}>
              点击下方按钮添加您的孩子
            </Text>
            <Button
              variant="primary"
              size="md"
              style={styles.fullButton}
              onPress={handleAddPress}
            >
              添加第一个孩子
            </Button>
          </ScrollView>
        ) : (
          <FlatList
            data={children ?? []}
            renderItem={renderItem}
            keyExtractor={(item) => item.id}
            refreshControl={
              <RefreshControl refreshing={refreshing} onRefresh={handleRefresh} />
            }
            ListHeaderComponent={
              <View style={styles.listHeader}>
                <Mascot size="md" />
                <Text style={[styles.subtitle, { color: theme.palette.neutral[500] }]}>
                  这里是你的孩子们
                </Text>
              </View>
            }
            ListFooterComponent={
              <Button
                variant="primary"
                size="md"
                style={styles.fullButton}
                onPress={handleAddPress}
                disabled={atQuota}
              >
                {`添加孩子（${childCount}/${QUOTA}）`}
              </Button>
            }
            contentContainerStyle={styles.listContent}
          />
        )}
      </SafeAreaView>
    </>
  )
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const styles = StyleSheet.create({
  container: { flex: 1 },
  flex1: { flex: 1 },
  centerBox: {
    flexGrow: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
  },
  emptyContent: {
    flexGrow: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
    gap: 16,
  },
  listContent: {
    padding: 16,
  },
  listHeader: {
    alignItems: 'center',
    paddingTop: 8,
    paddingBottom: 24,
    gap: 12,
  },
  subtitle: {
    fontSize: 14,
    textAlign: 'center',
  },
  fullButton: {
    width: '100%',
    marginTop: 16,
  },
  cardSpacing: {
    marginBottom: 12,
  },
  actionRow: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 12,
    paddingVertical: 10,
    gap: 8,
  },
  trashIconButton: {
    padding: 4,
    marginLeft: 4,
  },
})