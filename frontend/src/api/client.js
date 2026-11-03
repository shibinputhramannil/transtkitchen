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

export function createOrganization(name, timeZone) {
  return authRequest('POST', '/api/organizations/', timeZone ? { name, time_zone: timeZone } : { name })
}

function organizationPath(organizationId, rest = '') {
  return `/api/organizations/${encodeURIComponent(organizationId)}/${rest}`
}

function ingredientPath(organizationId, ingredientId, rest = '') {
  return organizationPath(organizationId, `ingredients/${encodeURIComponent(ingredientId)}/${rest}`)
}

export function getOrganization(organizationId) {
  return authRequest('GET', organizationPath(organizationId))
}

export function updateOrganization(organizationId, changes) {
  return authRequest('PATCH', organizationPath(organizationId), changes)
}

export function listMembers(organizationId) {
  return authRequest('GET', organizationPath(organizationId, 'members/'))
}

export function listIngredients(organizationId, { search = '', archived = false, page = 1 } = {}) {
  const params = new URLSearchParams({ page: String(page) })
  if (search) params.set('search', search)
  if (archived) params.set('archived', 'true')
  return authRequest('GET', organizationPath(organizationId, `ingredients/?${params}`))
}

export function createIngredient(organizationId, ingredient) {
  return authRequest('POST', organizationPath(organizationId, 'ingredients/'), ingredient)
}

export function updateIngredient(organizationId, ingredientId, changes) {
  return authRequest('PATCH', ingredientPath(organizationId, ingredientId), changes)
}

// Stock changes carry a request ID chosen once per submission. Sending the
// same ID again (a retry after a lost response, or a token refresh) returns
// the change already recorded instead of applying it twice.
export function receiveStock(organizationId, ingredientId, { quantity, expiresOn, note, requestId }) {
  return authRequest('POST', ingredientPath(organizationId, ingredientId, 'receive/'), {
    quantity,
    expires_on: expiresOn || null,
    note,
    request_id: requestId,
  })
}

export function countStock(
  organizationId,
  ingredientId,
  { countedQuantity, expectedQuantity, note, requestId },
) {
  return authRequest('POST', ingredientPath(organizationId, ingredientId, 'count/'), {
    counted_quantity: countedQuantity,
    expected_quantity: expectedQuantity,
    note,
    request_id: requestId,
  })
}

export function listBatches(organizationId, ingredientId) {
  return authRequest('GET', ingredientPath(organizationId, ingredientId, 'batches/'))
}

// Pass null as expiresOn to record that the batch has no expiry date.
export function correctBatchExpiry(organizationId, ingredientId, batchId, { expiresOn, note }) {
  return authRequest(
    'PATCH',
    ingredientPath(organizationId, ingredientId, `batches/${encodeURIComponent(batchId)}/`),
    { expires_on: expiresOn, note },
  )
}

export function listExpiring(organizationId, within = 3) {
  return authRequest('GET', organizationPath(organizationId, `expiring/?within=${within}`))
}
