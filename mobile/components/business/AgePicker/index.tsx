/**
 * AgePicker — discrete slider for selecting a child's age (3–21 years).
 *
 * Built on top of the M4.6 DiscreteSlider component.
 * Exports `formatAgeLabel(age)` as a utility for use in the surrounding UI
 * (AgePicker boundary labels only — list cards MUST NOT use this function;
 * they render plain "{age}岁" from birthDateToAge).
 *
 * Labels are rendered outside DiscreteSlider in a flex row to guarantee
 * visibility at boundary nodes (the slider's internal bottomRow with
 * negative margin can clip labels at extreme thumb positions).
 *
 * Usage:
 *   const [age, setAge] = useState(12)
 *   <AgePicker value={age} onValueChange={setAge} />
 */
import { View, Text, StyleSheet } from 'react-native'
import { DiscreteSlider } from '@/components/ui/DiscreteSlider'
import { useTheme } from '@/theme'

/** Age nodes: 3, 4, 5, …, 21 (inclusive). */
export const AGE_NODES = Array.from({ length: 19 }, (_, i) => i + 3) // [3..21]

/**
 * Format an age value for display in the slider's centre label.
 * Used ONLY inside AgePicker and its surrounding UI (left/right boundary labels).
 * List cards render plain "{age}岁" — they MUST NOT call this function.
 */
export function formatAgeLabel(age: number): string {
  if (age <= 3) return '3-'
  if (age >= 21) return '20+'
  return `${age}岁`
}

interface AgePickerProps {
  value: number
  onValueChange: (age: number) => void
  disabled?: boolean
}

export function AgePicker({ value, onValueChange, disabled = false }: AgePickerProps) {
  const theme = useTheme()

  return (
    <View style={labelStyles.row}>
      {/* Left boundary label — rendered outside DiscreteSlider */}
      <Text style={[labelStyles.boundary, { color: theme.palette.neutral[500] }]}>
        {formatAgeLabel(AGE_NODES[0])}
      </Text>

      <View style={{ flex: 1 }}>
        <DiscreteSlider
          nodes={AGE_NODES}
          value={value}
          onValueChange={onValueChange}
          disabled={disabled}
          centerLabel={formatAgeLabel(value)}
          showLeftLabel={false}
          showRightLabel={false}
          showCenterLabel={false}
        />
      </View>

      {/* Right boundary label — rendered outside DiscreteSlider */}
      <Text style={[labelStyles.boundary, { color: theme.palette.neutral[500] }]}>
        {formatAgeLabel(AGE_NODES[AGE_NODES.length - 1])}
      </Text>
    </View>
  )
}

const labelStyles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  boundary: {
    fontSize: 12,
    minWidth: 24,
    textAlign: 'center',
  },
})

