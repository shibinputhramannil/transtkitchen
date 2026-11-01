// The access and refresh tokens live only in this module's memory. They are
// never written to localStorage, sessionStorage, or cookies, so they cannot be
// read back from browser storage, and reloading or closing the tab signs the
// user out.
let tokens = null
const listeners = new Set()

export function getTokens() {
  return tokens
}

export function setTokens(next) {
  tokens = next ? { access: next.access, refresh: next.refresh } : null
  listeners.forEach((listener) => listener(tokens))
}

export function clearTokens() {
  setTokens(null)
}

// Calls `listener` whenever the tokens change; returns an unsubscribe function.
export function subscribe(listener) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
