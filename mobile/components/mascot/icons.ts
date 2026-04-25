// M4.7 Step 3 临时 stub — Step 5 替换为真实实现
// 本文件在 Step 5 完成后删除
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

export const THINKING_LAYOUT: ThinkingItem[] = []
export const NARRATING_LAYOUT: NarratingItem[] = []
