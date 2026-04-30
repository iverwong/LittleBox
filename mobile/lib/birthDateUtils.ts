/**
 * birthDateToAge — timezone-safe age calculator.
 *
 * Parses a "YYYY-MM-DD" date string and computes the integer age
 * in the local timezone, without using `new Date(dateStr)` (which
 * parses in the browser's local timezone and can shift by ±1 day
 * near UTC midnight).
 *
 * Algorithm:
 * 1. Split "YYYY-MM-DD" → [year, month, day] as numbers.
 * 2. Compute `today.getFullYear() - birthYear`, then subtract one
 *    more if today's month-day is before the birth month-day.
 *    We encode "MMDD" as `m * 100 + d` to compare two dates safely.
 *
 * Edge cases handled:
 * - Birthday today  → todayMd === birthMd → no subtraction → correct.
 * - Born Feb 29 on a non-leap year → birthMd = 229; today (Mar 1) = 301;
 *   todayMd > birthMd → no subtraction → still correct.
 */
export function birthDateToAge(dateStr: string): number {
  const parts = dateStr.split('-').map(Number)
  const [y, m, d] = parts
  const today = new Date()
  let age = today.getFullYear() - y
  const todayMd = (today.getMonth() + 1) * 100 + today.getDate()
  const birthMd = m * 100 + d
  if (todayMd < birthMd) age--
  return age
}
