// Step 5.1 fix — 将 SVG 非 path 元素（<circle>）转换为合法 path d 命令
// 转换规则：
//   circle cx="X" cy="Y" r="R"  →  M{X-R} {Y} a{R} {R} 0 1 0 {2R} 0 a{R} {R} 0 1 0 {-2R} 0
// path / line / polyline / polygon → 直接保留 d 字符串（SVG 已合规）

export interface ThinkingItem {
	iconD: string
	startX: number
	peakX: number
	startY: number
	peakY: number
	scaleStart: number
	scaleEnd: number
	rotateEnd: number
	delay: number
	durationMs: number
}

export interface NarratingItem {
	iconD: string
	x: number
	startY: number
	endY: number
	scaleStart: number
	scaleEnd: number
	delay: number
	durationMs: number
}

// ─── thinking 图标（24×24 viewBox）───

/** audio-lines: 6 条垂直音频柱（纯 path） */
export const ICON_THINKING_AUDIO_LINES =
	"M2 10v3 M6 6v11 M10 3v18 M14 8v7 M18 5v13 M22 10v3"

/** binoculars: 双目镜造型（纯 path） */
export const ICON_THINKING_BINOCULARS =
	"M10 10h4 M19 7V4a1 1 0 0 0-1-1h-2a1 1 0 0 0-1 1v3 M20 21a2 2 0 0 0 2-2v-3.851c0-1.39-2-2.962-2-4.829V8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v11a2 2 0 0 0 2 2z M 22 16 L 2 16 M4 21a2 2 0 0 1-2-2v-3.851c0-1.39 2-2.962 2-4.829V8a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v11a2 2 0 0 1-2 2z M9 7V4a1 1 0 0 0-1-1H6a1 1 0 0 0-1 1v3"

/**
 * cog: 齿轮
 * 含 2 个 <circle>（r=2 和 r=8），已转换为 arc 命令
 * 其余 12 条 path 段直接保留
 */
export const ICON_THINKING_COG =
	"M11 10.27 L7 3.34 " +
	"m11 13.73 -4 6.93 " +
	"M12 22v-2 " +
	"M12 2v2 " +
	"M14 12h8 " +
	"m17 20.66-1-1.73 " +
	"m17 3.34-1 1.73 " +
	"M2 12h2 " +
	"m20.66 17-1.73-1 " +
	"m20.66 7-1.73 1 " +
	"m3.34 17 1.73-1 " +
	"m3.34 7 1.73 1 " +
	/* circle r=2 cx=12 cy=12 → 2 个 arc 拼成完整圆 */
	"M10 12 a2 2 0 1 0 4 0 a2 2 0 1 0 -4 0 " +
	/* circle r=8 cx=12 cy=12 → 2 个 arc 拼成完整圆 */
	"M4 12 a8 8 0 1 0 16 0 a8 8 0 1 0 -16 0"

/**
 * file-search: 文件 + 放大镜
 * 含 1 个 <circle cx="11.5" cy="14.5" r="2.5">，已转换为 arc 命令
 * 其余 3 个 path 段直接保留
 */
export const ICON_THINKING_FILE_SEARCH =
	"M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z " +
	"M14 2v5a1 1 0 0 0 1 1h5 " +
	/* circle cx=11.5 cy=14.5 r=2.5 → 2 个 arc 拼成完整圆 */
	"M9 14.5 a2.5 2.5 0 1 0 5 0 a2.5 2.5 0 1 0 -5 0 " +
	"M13.3 16.3 15 18"

/** heart-pulse: 心跳曲线（纯 path） */
export const ICON_THINKING_HEART_PULSE =
	"M2 9.5a5.5 5.5 0 0 1 9.591-3.676.56.56 0 0 0 .818 0A5.49 5.49 0 0 1 22 9.5c0 2.29-1.5 4-3 5.5l-5.492 5.313a2 2 0 0 1-3 .019L5 15c-1.5-1.5-3-3.2-3-5.5 " +
	"M3.22 13H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"

/** lightbulb: 灯泡（纯 path） */
export const ICON_THINKING_LIGHTBULB =
	"M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5 " +
	"M9 18h6 " +
	"M10 22h4"

/** wrench: 扳手（纯 path） */
export const ICON_THINKING_WRENCH =
	"M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"

// ─── narrating 光斑（手写 4 角十字星，24×24 viewBox）───

export const ICON_NARRATING_GLOW =
	"M12 2 L14 10 L22 12 L14 14 L12 22 L10 14 L2 12 L10 10 Z"

// ─── 布局参数（错峰 200ms / 单次 1800ms）───

export const NARRATING_LAYOUT: NarratingItem[] = [
	{ iconD: ICON_NARRATING_GLOW, x: 74.7,  startY: 125.2, endY: 35, scaleStart: 1, scaleEnd: 0.6, delay: 0,   durationMs: 1000 },
	{ iconD: ICON_NARRATING_GLOW, x: 66.7,  startY: 125.2, endY: 30, scaleStart: 1, scaleEnd: 0.5, delay: 250, durationMs: 1000 },
	{ iconD: ICON_NARRATING_GLOW, x: 82.7,  startY: 125.2, endY: 32, scaleStart: 1, scaleEnd: 0.5, delay: 500, durationMs: 1000 },
]

export const THINKING_LAYOUT: ThinkingItem[] = [
	{ iconD: ICON_THINKING_AUDIO_LINES,  startX: 88,  peakX: 68,  startY: 125, peakY: 30, scaleStart: 1, scaleEnd: 0.7, rotateEnd: -25, delay: 0,    durationMs: 1800 },
	{ iconD: ICON_THINKING_BINOCULARS,   startX: 112, peakX: 132, startY: 125, peakY: 25, scaleStart: 1, scaleEnd: 0.7, rotateEnd:  20, delay: 200,  durationMs: 1800 },
	{ iconD: ICON_THINKING_COG,          startX: 92,  peakX: 72,  startY: 125, peakY: 35, scaleStart: 1, scaleEnd: 0.7, rotateEnd: -18, delay: 400,  durationMs: 1800 },
	{ iconD: ICON_THINKING_FILE_SEARCH,  startX: 108, peakX: 128, startY: 125, peakY: 28, scaleStart: 1, scaleEnd: 0.7, rotateEnd:  15, delay: 600,  durationMs: 1800 },
	{ iconD: ICON_THINKING_HEART_PULSE,  startX: 90,  peakX: 70,  startY: 125, peakY: 32, scaleStart: 1, scaleEnd: 0.7, rotateEnd: -12, delay: 800,  durationMs: 1800 },
	{ iconD: ICON_THINKING_LIGHTBULB,    startX: 110, peakX: 130, startY: 125, peakY: 33, scaleStart: 1, scaleEnd: 0.7, rotateEnd:  18, delay: 1000, durationMs: 1800 },
	{ iconD: ICON_THINKING_WRENCH,       startX: 100, peakX: 85,  startY: 125, peakY: 24, scaleStart: 1, scaleEnd: 0.7, rotateEnd:  -8, delay: 1200, durationMs: 1800 },
]
