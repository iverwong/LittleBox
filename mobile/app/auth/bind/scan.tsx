import { CameraView, useCameraPermissions } from 'expo-camera'
import { Stack } from 'expo-router'
import { useEffect, useRef, useState } from 'react'
import { Linking, StyleSheet, Text, View } from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'

import { Button, EmptyState } from '@/components/ui'
import { useBindRedeem } from '@/hooks/useBindRedeem'

type ScanStatus = 'idle' | 'pending' | 'invalid'

export default function ScanScreen() {
  const [permission, requestPermission] = useCameraPermissions()
  const scanLock = useRef(false)
  const failedTokensRef = useRef<Set<string>>(new Set())
  const [scanStatus, setScanStatus] = useState<ScanStatus>('idle')

  const { redeem, isPending } = useBindRedeem({
    onError: (failedToken) => {
      failedTokensRef.current.add(failedToken)
      setScanStatus('invalid')
      scanLock.current = false
    },
  })

  // 进页面自动请求一次权限
  useEffect(() => {
    if (permission && !permission.granted && permission.canAskAgain) {
      requestPermission()
    }
  }, [permission])

  // 权限尚未确定 → 等待
  if (!permission) {
    return (
      <SafeAreaView style={styles.container} edges={['top', 'bottom']}>
        <Stack.Screen options={{ title: '扫码绑定' }} />
        <View style={styles.centerBox}>
          <Text style={styles.hintText}>正在请求相机权限…</Text>
        </View>
      </SafeAreaView>
    )
  }

  // 权限被永久拒绝 → 唯一兜底：前往系统设置开放权限
  if (!permission.granted && !permission.canAskAgain) {
    return (
      <SafeAreaView style={styles.container} edges={['top', 'bottom']}>
        <Stack.Screen options={{ title: '扫码绑定' }} />
        <View style={styles.centerBox}>
          <EmptyState
            icon="camera-off"
            title="未授权使用相机"
            description="绑定子账号需要扫描家长出示的二维码，请前往系统设置允许「小盒子」使用相机。"
            action={
              <Button
                variant="primary"
                onPress={() => Linking.openSettings()}
              >
                前往系统设置
              </Button>
            }
          />
        </View>
      </SafeAreaView>
    )
  }

  // 权限询问中（首次进入弹系统弹窗那一刻）
  if (!permission.granted) {
    return (
      <SafeAreaView style={styles.container} edges={['top', 'bottom']}>
        <Stack.Screen options={{ title: '扫码绑定' }} />
        <View style={styles.centerBox}>
          <Text style={styles.hintText}>请允许使用相机以扫描绑定码</Text>
        </View>
      </SafeAreaView>
    )
  }

  // 权限已授予 → 显示相机
  return (
    <SafeAreaView style={styles.cameraContainer} edges={['bottom']}>
      <Stack.Screen options={{ title: '扫码绑定' }} />
      <CameraView
        style={StyleSheet.absoluteFill}
        facing="back"
        barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
        onBarcodeScanned={({ data }) => {
          if (scanLock.current || isPending) return
          if (failedTokensRef.current.has(data)) return // 已知失败码，静默跳过
          scanLock.current = true
          setScanStatus('pending')
          redeem(data)
        }}
      />
      <View style={styles.overlay} pointerEvents="none">
        <View
          style={[
            styles.scanBox,
            scanStatus === 'invalid' && styles.scanBoxInvalid,
          ]}
        />
        <Text
          style={[
            styles.tipText,
            scanStatus === 'invalid' && styles.tipTextInvalid,
          ]}
        >
          {scanStatus === 'invalid'
            ? '绑定码无效或已失效，请家长重新生成后再扫'
            : scanStatus === 'pending'
              ? '正在校验绑定码…'
              : '对准家长出示的绑定二维码'}
        </Text>
      </View>
    </SafeAreaView>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#F2EADF',
  },
  cameraContainer: {
    flex: 1,
    backgroundColor: '#000',
  },
  centerBox: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    paddingHorizontal: 32,
  },
  hintText: {
    fontSize: 14,
    color: '#998260',
  },
  overlay: {
    ...StyleSheet.absoluteFillObject,
    justifyContent: 'center',
    alignItems: 'center',
    backgroundColor: 'rgba(0,0,0,0.35)',
  },
  scanBox: {
    width: 240,
    height: 240,
    borderWidth: 2,
    borderColor: '#F2EADF',
    borderRadius: 16,
  },
  tipText: {
    color: '#F2EADF',
    fontSize: 14,
    marginTop: 24,
  },
  scanBoxInvalid: {
    borderColor: '#EF4444', // 偏暖红，与米杏色系不冲突；后期可对齐 theme.ui.error token
  },
  tipTextInvalid: {
    color: '#FCA5A5', // 浅红，在深背景下清晰可读
  },
})