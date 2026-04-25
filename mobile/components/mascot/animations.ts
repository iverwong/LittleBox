import { Easing } from 'react-native-reanimated'
import type { MascotState } from './Mascot.types'

export interface BodyParams {
    scaleX: number       // 循环态：峰值；非循环：终态
    scaleY: number
    translateY: number
    duration: number     // 循环态：单次半周期 ms；非循环：达到终态的 ms
    loop: boolean
}

export interface StateParams {
    body: BodyParams
    eyes: 'open' | 'squint' | 'smile' | 'crescent'
    thinkingActive: boolean
    narratingActive: boolean
    blinkActive: boolean   // idle 独立眨眼开关
    isOneTime: boolean
    oneTimeDurationMs?: number
}

/**
 * 6 态动效参数表（首版可运行参数；6 态各自动作专题确认后回写本表）。
 * - idle:      scale 1↔1.02 / translateY 0↔-2 呼吸 1.75s 半周期 + 眨眼 4s 周期
 * - listen:    scale 1.04 / translateY 0 静态收紧（250ms 平滑过渡），squint
 * - thinking:  body 静止，4 元素抛物线抛出（详见 icons.ts THINKING_LAYOUT），crescent
 * - narrating: scale 1↔1.03 / translateY 0↔-1 高频弹动 0.3s 半周期 + smile + 光斑直上
 * - enter:     由 Mascot.tsx enter 序列驱动（spring fall + squash & stretch + shadow grow），open
 * - done:      scale 1↔1.05↔1 轻颔首 500ms，crescent 月牙
 */
export const STATE_PARAMS: Record<MascotState, StateParams> = {
    idle: {
        body: { scaleX: 1.02, scaleY: 1.02, translateY: -2, duration: 1750, loop: true },
        eyes: 'open',
        thinkingActive: false,
        narratingActive: false,
        blinkActive: true,
        isOneTime: false,
    },
    listen: {
        body: { scaleX: 1.04, scaleY: 1.04, translateY: 0, duration: 250, loop: false },
        eyes: 'squint',
        thinkingActive: false,
        narratingActive: false,
        blinkActive: false,
        isOneTime: false,
    },
    thinking: {
        body: { scaleX: 1, scaleY: 1, translateY: 0, duration: 250, loop: false },
        eyes: 'crescent',
        thinkingActive: true,
        narratingActive: false,
        blinkActive: false,
        isOneTime: false,
    },
    narrating: {
        body: { scaleX: 1.03, scaleY: 1.03, translateY: -1, duration: 300, loop: true },
        eyes: 'smile',
        thinkingActive: false,
        narratingActive: true,
        blinkActive: false,
        isOneTime: false,
    },
    enter: {
        body: { scaleX: 1, scaleY: 1, translateY: 0, duration: 1200, loop: false },
        eyes: 'open',
        thinkingActive: false,
        narratingActive: false,
        blinkActive: false,
        isOneTime: true,
        oneTimeDurationMs: 1200,
    },
    done: {
        body: { scaleX: 1, scaleY: 1, translateY: 0, duration: 500, loop: false },
        eyes: 'crescent',
        thinkingActive: false,
        narratingActive: false,
        blinkActive: false,
        isOneTime: true,
        oneTimeDurationMs: 500,
    },
}

export const EASING = {
    standard: Easing.inOut(Easing.ease),
    loopSoft: Easing.inOut(Easing.sin),
    bounce: Easing.out(Easing.back(1.5)),
    parabolic: Easing.out(Easing.quad),  // thinking 抛物线上升
    fall: Easing.in(Easing.quad),         // enter 坠落
}

/**
 * 眼睛 4 形态 d（基于 viewBox 200×200，盒脸中心 y≈100；左眼中心 (88,100)，右眼中心 (112,100)）。
 * 首版草案：Iver 真机看到 mascot.svg 实际盒脸位置后，按实际坐标微调。
 */
export const EYE_OPEN_D     = "M82 100 a6 6 0 1 0 12 0 a6 6 0 1 0 -12 0 M106 100 a6 6 0 1 0 12 0 a6 6 0 1 0 -12 0"
export const EYE_SQUINT_D   = "M82 100 h12 M106 100 h12"
export const EYE_SMILE_D    = "M82 96 q6 8 12 0 M106 96 q6 8 12 0"
export const EYE_CRESCENT_D = "M82 102 q6 -8 12 0 M106 102 q6 -8 12 0"

// idle 眨眼参数：scaleY 1 → 0.1 → 1，单次 200ms，每 4s 触发一次
export const BLINK_PERIOD_MS = 4000
export const BLINK_DURATION_MS = 200
