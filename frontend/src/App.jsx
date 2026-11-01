import { useEffect, useState } from 'react'
import { getHealth } from './api/client.js'
import './App.css'

// Resolves to { health, error } instead of throwing, because a 503 still
// carries a useful body describing which check failed.
async function loadHealth() {
  try {
    return { health: await getHealth(), error: null }
  } catch (err) {
    return { health: err.data ?? null, error: err.message }
  }
}

function StatusRow({ label, value }) {
  const ok = value === 'ok'
  return (
    <div className="status-row">
      <span>{label}</span>
      <span className={`badge ${ok ? 'badge-ok' : 'badge-error'}`}>{value}</span>
    </div>
  )
}

function App() {
  const [result, setResult] = useState({ health: null, error: null })
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    loadHealth().then((next) => {
      if (!cancelled) {
        setResult(next)
        setLoading(false)
      }
    })
    return () => {
      cancelled = true
    }
  }, [])

  async function handleRetry() {
    setLoading(true)
    setResult(await loadHealth())
    setLoading(false)
  }

  const { health, error } = result

  return (
    <main className="app">
      <header>
        <h1>KitchenMind AI</h1>
        <p className="tagline">
          Kitchen operations for independent restaurants, bakeries, and cloud kitchens.
        </p>
      </header>

      <section className="card" aria-live="polite">
        <h2>System status</h2>
        {loading && <p>Checking API…</p>}
        {!loading && health && (
          <>
            <StatusRow label="API" value={health.status} />
            <StatusRow label="Database" value={health.database} />
          </>
        )}
        {!loading && error && <p className="error">Could not reach the API: {error}</p>}
        <button type="button" onClick={handleRetry} disabled={loading}>
          Check again
        </button>
      </section>
    </main>
  )
}

export default App
