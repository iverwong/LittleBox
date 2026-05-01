/**
 * AgePicker — discrete slider for selecting a child's age (3–21 years).
 *
 * Built on top of the DiscreteSlider component.
 * 两端边界 (3 / 21) 不再做特殊语义 — 直接渲染字面年龄，与 value 一致。
 * 与 birthDateToAge 推算结果对齐，跨年自然增长，不会出现「20+ ↔ 21 ↔ 22」错位。
 *
 * Usage:
 *   const [age, setAge] = useState(12)
 *   <AgePicker value={age} onValueChange={setAge} />
 */
import { DiscreteSlider } from '@/components/ui/DiscreteSlider'

/** Age nodes: 3, 4, 5, …, 21 (inclusive). */
export const AGE_NODES = Array.from({ length: 19 }, (_, i) => i + 3) // [3..21]

interface AgePickerProps {
  value: number
  onValueChange: (age: number) => void
  disabled?: boolean
}

export function AgePicker({ value, onValueChange, disabled = false }: AgePickerProps) {
  return (
    <DiscreteSlider
      nodes={AGE_NODES}
      value={value}
      onValueChange={onValueChange}
      disabled={disabled}
      centerLabel={`${value}岁`}
      showLeftLabel={true}
      showRightLabel={true}
      showCenterLabel={true}
    />
  )
}