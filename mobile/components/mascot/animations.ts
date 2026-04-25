/**
 * Mascot SVG animations — MVP simplified
 * Only blink animation retained; all state machine logic removed.
 */

export const EYE_CENTER_Y = 134.0  // approximate midpoint (two eye y differ slightly)

export const EYE_OPEN_D = "M85.5 135.4 a6 6 0 1 0 12 0 a6 6 0 1 0 -12 0 M54.1 132.5 a6 6 0 1 0 12 0 a6 6 0 1 0 -12 0"

// Blink randomization parameters
export const BLINK_INTERVAL_MIN_MS = 2500
export const BLINK_INTERVAL_MAX_MS = 5500
export const BLINK_DOUBLE_PROB = 0.25          // probability of double blink
export const BLINK_CLOSE_MS = 90                // scaleY 1 → 0.1 duration
export const BLINK_OPEN_MS = 90                // scaleY 0.1 → 1 duration (single blink)
export const BLINK_DOUBLE_GAP_MS = 80          // eyes briefly open between two blinks
