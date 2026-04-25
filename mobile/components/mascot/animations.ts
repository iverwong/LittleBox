/**
 * Mascot SVG animations — MVP simplified
 * Only blink animation retained; all state machine logic removed.
 */

export const EYE_CENTER_Y = 134.0  // approximate midpoint (two eye y differ slightly)

export const EYE_OPEN_D = "M85.5 135.4 a6 6 0 1 0 12 0 a6 6 0 1 0 -12 0 M54.1 132.5 a6 6 0 1 0 12 0 a6 6 0 1 0 -12 0"

// idle blink: scaleY 1 → 0.1 → 1, single blink 200ms, every 4s
export const BLINK_PERIOD_MS = 4000
export const BLINK_DURATION_MS = 200
