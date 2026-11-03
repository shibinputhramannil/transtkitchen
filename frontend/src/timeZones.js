export function browserTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null
  } catch {
    return null
  }
}

// Today's date in `timeZone` as YYYY-MM-DD, the format of date inputs and the API.
export function localDate(timeZone) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date())
  const part = (type) => parts.find((item) => item.type === type).value
  return `${part('year')}-${part('month')}-${part('day')}`
}

// Time zones the browser knows, always including UTC and `current`.
export function timeZoneOptions(current) {
  let names = []
  try {
    names = Intl.supportedValuesOf('timeZone')
  } catch {
    // Older browsers: offer only UTC and the current value.
  }
  return [...new Set(['UTC', current, ...names])].filter(Boolean).sort()
}
