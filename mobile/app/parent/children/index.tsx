/**
 * parent/children/index.tsx — children list page (M5 F4).
 *
 * Features:
 * - FlatList with skeleton loading state (3 placeholder cards)
 * - Pull-to-refresh + useFocusEffect refetch
 * - Child cards: GenderAvatar + nickname·age + chevron + primary action button + trash
 * - + button (header right): quota guard → Toast or navigate to /parent/children/new
 * - All three onPress stubs (ListItem / primary button / trash) are toast-only in F4;
 *   F5 will wire them to real modals.
 *
 * API contract (backend M4.8):
 *   GET /children  → { children: ChildSummary[] }
 *   ChildSummary: { id, nickname, birth_date, gender, is_bound }
 */
import { useCallback, useRef, useState } from 'react'
import {
  View,
  Text,
  FlatList,
  RefreshControl,
  Pressable,
  StyleSheet,
} from 'react-native'
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
// Skeleton card (shown while loading)
// ---------------------------------------------------------------------------

function SkeletonCard() {
  const theme = useTheme()
  return (
    <Card variant="outlined" padding={0} style={{ marginBottom: theme.spacing[3] }}>
      <View style={[styles.skeletonInfoRow, { backgroundColor: theme.palette.neutral[100] }]} />
      <View style={[styles.skeletonActionRow, { backgroundColor: theme.palette.neutral[50] }]} />
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Child card
// ---------------------------------------------------------------------------

interface ChildCardProps {
  child: ChildSummary
}

function ChildCard({ child }: ChildCardProps) {
  const theme = useTheme()
  const age = birthDateToAge(child.birth_date)
  const primaryActionLabel = child.is_bound ? '下线设备' : '绑定设备'
  const redline = theme.report.redline

  const handleListItemPress = useCallback(() => {
    toast.show({ message: '孩子详情页 F5 上线', variant: 'info', duration: 1500 })
  }, [])

  const handlePrimaryAction = useCallback(() => {
    toast.show({ message: '绑定功能开发中', variant: 'info', duration: 1500 })
  }, [])

  const handleTrashPress = useCallback(() => {
    toast.show({ message: '删除功能开发中', variant: 'info', duration: 1500 })
  }, [])

  return (
    <Card variant="outlined" padding={0} style={{ marginBottom: theme.spacing[3] }}>
      {/* 信息区：整块 Pressable → settings 占位 */}
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
      {/* 操作行：Card 的子兄弟元素，不在 ListItem 内，零嵌套 */}
      <View style={styles.actionRow}>
        <Button
          variant="primary"
          size="md"
          style={{ flex: 1 }}
          onPress={handlePrimaryAction}
        >
          {primaryActionLabel}
        </Button>
        <Pressable
          onPress={handleTrashPress}
          hitSlop={8}
          style={styles.trashIconButton}
        >
          <Ionicons name="trash-outline" size={20} color={redline} />
        </Pressable>
      </View>
    </Card>
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

  // In-flight request controller for cleanup on unmount / re-run
  const abortRef = useRef<AbortController | null>(null)

  const fetchChildren = useCallback(async () => {
    if (abortRef.current) abortRef.current.abort()
    const controller = new AbortController()
    abortRef.current = controller

    const res = await api.get<{ children: ChildSummary[] }>('/children')
    if (!res.ok) {
      // 4xx / 5xx — non-ok response
      if (!controller.signal.aborted) {
        setError(true)
        setLoading(false)
      }
      return
    }
    if (!controller.signal.aborted) {
      setChildren(res.data.children)
      setLoading(false)
      setError(false)
    }
  }, [])

  // useFocusEffect: re-fetch whenever the screen gains focus
  // (covers return from new.tsx / modal close)
  useFocusEffect(
    useCallback(() => {
      setLoading(true)
      setError(false)
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
    if ((children?.length ?? 0) >= QUOTA) {
      toast.show({ message: '最多 3 个孩子，请先删除已有', variant: 'error', duration: 3000 })
      return
    }
    ;(router.push as (href: string) => void)('/parent/children/new')
  }, [children, router])

  const renderItem = useCallback(
    ({ item }: { item: ChildSummary }) => <ChildCard child={item} />,
    [],
  )

  const renderSkeleton = useCallback(
    () => (
      <View>
        <SkeletonCard />
        <SkeletonCard />
        <SkeletonCard />
      </View>
    ),
    [],
  )

  const renderEmpty = useCallback(() => {
    if (loading || error) return null
    return (
      <View style={styles.emptyContainer}>
        <Mascot size="lg" />
        <Text style={[styles.emptyText, { color: theme.palette.neutral[400] }]}>
          点右上 + 添加你的第一个孩子
        </Text>
      </View>
    )
  }, [loading, error, theme])

  const renderError = useCallback(() => {
    if (!error) return null
    return (
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
    )
  }, [error, fetchChildren])

  const atQuota = (children?.length ?? 0) >= QUOTA

  return (
    <>
      <Stack.Screen
        options={{
          title: '我的孩子',
          headerRight: () => (
            <Pressable
              onPress={handleAddPress}
              hitSlop={8}
              disabled={atQuota}
              style={{ opacity: atQuota ? 0.4 : 1 }}
            >
              <Ionicons
                name="add"
                size={24}
                color={theme.palette.secondary[700]}
              />
            </Pressable>
          ),
        }}
      />

      <View style={[styles.container, { backgroundColor: theme.surface.paper }]}>
        {loading && !refreshing ? (
          <FlatList
            data={[]}
            renderItem={renderItem}
            ListHeaderComponent={renderSkeleton}
            keyExtractor={() => 'skeleton'}
            contentContainerStyle={styles.listContent}
          />
        ) : (
          <FlatList
            data={children ?? []}
            renderItem={renderItem}
            ListHeaderComponent={renderError}
            ListEmptyComponent={renderEmpty}
            keyExtractor={(item) => item.id}
            refreshControl={
              <RefreshControl refreshing={refreshing} onRefresh={handleRefresh} />
            }
            contentContainerStyle={[
              styles.listContent,
              (children?.length ?? 0) === 0 && styles.listContentEmpty,
            ]}
          />
        )}
      </View>
    </>
  )
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const styles = StyleSheet.create({
  container: { flex: 1 },
  listContent: { padding: 16 },
  listContentEmpty: { flex: 1 },
  skeletonInfoRow: { height: 72 },
  skeletonActionRow: { height: 52 },
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
  emptyContainer: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: 80,
    gap: 16,
  },
  emptyText: {
    fontSize: 14,
    textAlign: 'center',
  },
})
