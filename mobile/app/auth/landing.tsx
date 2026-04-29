import { View, Text, StyleSheet, Linking } from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'
import { router } from 'expo-router'
import { Mascot } from '@/components/mascot/Mascot'
import { Button } from '@/components/ui/Button/Button'

export default function LandingScreen() {
  return (
    <SafeAreaView style={styles.container}>
      <View style={styles.content}>
        {/* Mascot */}
        <View style={styles.mascotWrap}>
          <Mascot size="xl" state="idle" />
        </View>

        {/* Brand */}
        <Text style={styles.brand}>小盒子</Text>
        <Text style={styles.tagline}>家长与孩子的 AI 助手</Text>

        {/* Actions */}
        <View style={styles.actions}>
          <Button
            variant="primary"
            size="lg"
            style={styles.button}
            onPress={() => router.push('/auth/login')}
          >
            我是家长
          </Button>
          <Button
            variant="secondary"
            size="lg"
            style={styles.button}
            onPress={() => router.push('/auth/bind/scan')}
          >
            我是孩子 · 扫码登录
          </Button>
        </View>

        {/* Footer */}
        <View style={styles.footer}>
          <Text style={styles.footerText}>
            登录即表示同意{` `}
            <Text
              style={styles.link}
              onPress={() => Linking.openURL('https://example.com/terms')}
            >
              《服务条款》
            </Text>
            {` `}和{` `}
            <Text
              style={styles.link}
              onPress={() => Linking.openURL('https://example.com/privacy')}
            >
              《隐私政策》
            </Text>
          </Text>
        </View>
      </View>
    </SafeAreaView>
  )
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#F2EADF',
  },
  content: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 32,
    paddingBottom: 40,
  },
  mascotWrap: {
    marginBottom: 24,
  },
  brand: {
    fontSize: 36,
    fontWeight: '700',
    color: '#6D4627',
    marginBottom: 8,
    letterSpacing: 2,
  },
  tagline: {
    fontSize: 16,
    color: '#7A6546',
    marginBottom: 48,
  },
  actions: {
    width: '100%',
    gap: 16,
  },
  button: {
    width: '100%',
  },
  footer: {
    position: 'absolute',
    bottom: 40,
    left: 0,
    right: 0,
    alignItems: 'center',
    paddingHorizontal: 24,
  },
  footerText: {
    fontSize: 12,
    color: '#998260',
    textAlign: 'center',
    lineHeight: 18,
  },
  link: {
    color: '#A67148',
    textDecorationLine: 'underline',
  },
})
