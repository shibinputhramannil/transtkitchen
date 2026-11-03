// "12.500" -> "12.5", "3.000" -> "3"; quantities stay strings, so no precision is lost.
export function formatQuantity(quantity) {
  return quantity.includes('.') ? quantity.replace(/0+$/, '').replace(/\.$/, '') : quantity
}

export const WASTE_REASONS = {
  expired: 'Expired',
  spoiled: 'Spoiled',
  damaged: 'Damaged (dropped or burnt)',
  overproduction: 'Overproduction',
  quality: 'Quality (returned or not to standard)',
  other: 'Other',
}

// Units each stock unit can be measured in: grams and kilograms, millilitres
// and litres, or counted items. The server enforces the same rule.
const UNIT_GROUPS = [['g', 'kg'], ['ml', 'l'], ['each']]

export function compatibleUnits(unit) {
  return UNIT_GROUPS.find((group) => group.includes(unit)) ?? [unit]
}
