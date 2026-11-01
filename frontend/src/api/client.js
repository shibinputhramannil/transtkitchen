import { clearTokens, getTokens, setTokens } from '../auth/tokens.js'

// In development the API defaults to the local Django server; production
// builds must set VITE_API_URL to the deployed API origin.
const API_URL = (
  import.meta.env.VITE_API_URL ?? (import.meta.env.DEV ? 'http://localhost:8000' : '')
).replace(/\/+$/, '')

function requestError(status, data = null) {
  const error = new Error(`Request failed with status ${status}`)
  error.status = status
  error.data = data
  return error
}

async function send(method, path, { body, token } = {}) {
  if (!API_URL) {
    throw new Error('VITE_API_URL is not configured')
  }

  const headers = { Accept: 'application/json' }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }

  const response = await fetch(`${API_URL}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = response.status === 204 ? null : await response.json().catch(() => null)

  if (!response.ok) {
    throw requestError(response.status, data)
  }
  return data
}

// Requests that find the access token expired share a single refresh.
let refreshing = null

function refreshTokens() {
  const current = getTokens()
  if (!current) {
    return Promise.reject(requestError(401))
  }
  refreshing ??= send('POST', '/api/auth/token/refresh/', { body: { refresh: current.refresh } })
    .then((next) => setTokens(next))
    .catch((error) => {
      clearTokens()
      throw error
    })
    .finally(() => {
      refreshing = null
    })
  return refreshing
}

// Sends a request as the signed-in user. If the access token has expired, it
// is refreshed once and the request retried; if that fails, the user is
// signed out.
async function authRequest(method, path, body) {
  const tokens = getTokens()
  if (!tokens) {
    throw requestError(401)
  }
  try {
    return await send(method, path, { body, token: tokens.access })
  } catch (error) {
    if (error.status !== 401) {
      throw error
    }
  }

  await refreshTokens()
  try {
    return await send(method, path, { body, token: getTokens()?.access })
  } catch (error) {
    if (error.status === 401) {
      clearTokens()
    }
    throw error
  }
}

// Turns an API error into messages to show the user.
export function errorMessages(error) {
  const data = error?.data
  if (data && typeof data === 'object') {
    if (typeof data.detail === 'string') {
      return [data.detail]
    }
    const messages = Object.values(data)
      .flat()
      .filter((message) => typeof message === 'string')
    if (messages.length) {
      return messages
    }
  }
  return [error?.status ? `Request failed (status ${error.status}).` : 'Could not reach the API.']
}

export function getHealth() {
  return send('GET', '/api/health/')
}

export function register({ email, password, firstName, lastName }) {
  return send('POST', '/api/auth/register/', {
    body: { email, password, first_name: firstName, last_name: lastName },
  })
}

export function login(email, password) {
  return send('POST', '/api/auth/token/', { body: { email, password } })
}

export function logout(refresh) {
  return send('POST', '/api/auth/logout/', { body: { refresh } })
}

export function getMe() {
  return authRequest('GET', '/api/auth/me/')
}

export function listOrganizations() {
  return authRequest('GET', '/api/organizations/')
}

export function createOrganization(name) {
  return authRequest('POST', '/api/organizations/', { name })
}

export function listMembers(organizationId) {
  return authRequest('GET', `/api/organizations/${encodeURIComponent(organizationId)}/members/`)
}
