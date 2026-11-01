// In development the API defaults to the local Django server; production
// builds must set VITE_API_URL to the deployed API origin.
const API_URL = (
  import.meta.env.VITE_API_URL ?? (import.meta.env.DEV ? 'http://localhost:8000' : '')
).replace(/\/+$/, '')

export async function apiGet(path) {
  if (!API_URL) {
    throw new Error('VITE_API_URL is not configured')
  }

  const response = await fetch(`${API_URL}${path}`, {
    headers: { Accept: 'application/json' },
  })
  const data = await response.json().catch(() => null)

  if (!response.ok) {
    const error = new Error(`Request failed with status ${response.status}`)
    error.status = response.status
    error.data = data
    throw error
  }
  return data
}

export function getHealth() {
  return apiGet('/api/health/')
}
