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

// A FormData body is sent as a file upload; anything else as JSON. With
// `text`, a successful response is returned as text instead of parsed JSON.
async function send(method, path, { body, token, text = false } = {}) {
  if (!API_URL) {
    throw new Error('VITE_API_URL is not configured')
  }

  const isForm = body instanceof FormData
  const headers = { Accept: text ? 'text/csv, application/json' : 'application/json' }
  if (body !== undefined && !isForm) {
    headers['Content-Type'] = 'application/json'
  }
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }

  const response = await fetch(`${API_URL}${path}`, {
    method,
    headers,
    body: body === undefined || isForm ? body : JSON.stringify(body),
  })
  if (response.ok && text) {
    return response.text()
  }
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
async function authRequest(method, path, body, { text = false } = {}) {
  const tokens = getTokens()
  if (!tokens) {
    throw requestError(401)
  }
  try {
    return await send(method, path, { body, token: tokens.access, text })
  } catch (error) {
    if (error.status !== 401) {
      throw error
    }
  }

  await refreshTokens()
  try {
    return await send(method, path, { body, token: getTokens()?.access, text })
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

// Every active ingredient, following pagination (for pickers such as the recipe editor).
export async function listAllIngredients(organizationId) {
  const all = []
  for (let page = 1; page <= 40; page += 1) {
    const data = await listIngredients(organizationId, { page })
    all.push(...data.results)
    if (!data.next) break
  }
  return all
}

function menuItemPath(organizationId, menuItemId, rest = '') {
  return organizationPath(organizationId, `menu-items/${encodeURIComponent(menuItemId)}/${rest}`)
}

export function listMenuItems(organizationId, { archived = false, page = 1 } = {}) {
  const params = new URLSearchParams({ page: String(page) })
  if (archived) params.set('archived', 'true')
  return authRequest('GET', organizationPath(organizationId, `menu-items/?${params}`))
}

export async function listAllMenuItems(organizationId) {
  const all = []
  for (let page = 1; page <= 40; page += 1) {
    const data = await listMenuItems(organizationId, { page })
    all.push(...data.results)
    if (!data.next) break
  }
  return all
}

export function createMenuItem(organizationId, name) {
  return authRequest('POST', organizationPath(organizationId, 'menu-items/'), { name })
}

export function updateMenuItem(organizationId, menuItemId, changes) {
  return authRequest('PATCH', menuItemPath(organizationId, menuItemId), changes)
}

// lines: [{ ingredient: id, quantity, unit }]; replaces the whole recipe.
export function saveRecipe(organizationId, menuItemId, lines) {
  return authRequest('PUT', menuItemPath(organizationId, menuItemId, 'recipe/'), { lines })
}

function salesImportPath(organizationId, importId, rest = '') {
  return organizationPath(organizationId, `sales-imports/${encodeURIComponent(importId)}/${rest}`)
}

export function uploadSalesImport(organizationId, file) {
  const form = new FormData()
  form.append('file', file)
  return authRequest('POST', organizationPath(organizationId, 'sales-imports/'), form)
}

export function listSalesImports(organizationId) {
  return authRequest('GET', organizationPath(organizationId, 'sales-imports/'))
}

export function getSalesImport(organizationId, importId) {
  return authRequest('GET', salesImportPath(organizationId, importId))
}

export function listSalesImportRows(organizationId, importId, status) {
  return authRequest('GET', salesImportPath(organizationId, importId, `rows/?status=${encodeURIComponent(status)}`))
}

// Map every row with this item name to a menu item (remembered for later
// imports), or pass menuItemId null to ignore those rows in this import.
export function mapSalesImportItem(organizationId, importId, itemName, menuItemId) {
  return authRequest(
    'POST',
    salesImportPath(organizationId, importId, 'mappings/'),
    menuItemId ? { item_name: itemName, menu_item: menuItemId } : { item_name: itemName, ignore: true },
  )
}

// Matches a draft's rows again against the current menu and remembered names.
// Viewing a preview never does this; repeating it changes nothing further.
export function rematchSalesImport(organizationId, importId) {
  return authRequest('POST', salesImportPath(organizationId, importId, 'rematch/'))
}

// Applying again after success returns the stored result, so retries are safe.
export function applySalesImport(organizationId, importId, acknowledged) {
  return authRequest('POST', salesImportPath(organizationId, importId, 'apply/'), { acknowledged })
}

export function discardSalesImport(organizationId, importId) {
  return authRequest('POST', salesImportPath(organizationId, importId, 'discard/'))
}

export function getSalesTemplate(organizationId) {
  return authRequest('GET', organizationPath(organizationId, 'sales-imports/template.csv'), undefined, {
    text: true,
  })
}

export function getDailySales(organizationId) {
  return authRequest('GET', organizationPath(organizationId, 'sales/daily/'))
}

// Logs waste. The request ID is chosen once per form, so submitting again
// after an error or a lost response never logs the waste twice.
export function logWaste(organizationId, entry) {
  return authRequest('POST', organizationPath(organizationId, 'waste/'), entry)
}

export function listWaste(organizationId, { includeVoided = false } = {}) {
  const params = includeVoided ? '?include_voided=true' : ''
  return authRequest('GET', organizationPath(organizationId, `waste/${params}`))
}

// Puts back the stock the entry took; voiding again changes nothing.
export function voidWaste(organizationId, entryId, reason) {
  return authRequest(
    'POST',
    organizationPath(organizationId, `waste/${encodeURIComponent(entryId)}/void/`),
    { reason },
  )
}

export function getWasteSummary(organizationId) {
  return authRequest('GET', organizationPath(organizationId, 'waste/summary/'))
}

function prepPlanPath(organizationId, planId, rest = '') {
  return organizationPath(organizationId, `prep-plans/${encodeURIComponent(planId)}/${rest}`)
}

export function getForecast(organizationId, date) {
  return authRequest('GET', organizationPath(organizationId, `forecast/?date=${encodeURIComponent(date)}`))
}

export function listPrepPlans(organizationId, { from, to } = {}) {
  const params = new URLSearchParams()
  if (from) params.set('from', from)
  if (to) params.set('to', to)
  return authRequest('GET', organizationPath(organizationId, `prep-plans/?${params}`))
}

export function createPrepPlan(organizationId, planDate) {
  return authRequest('POST', organizationPath(organizationId, 'prep-plans/'), { plan_date: planDate })
}

export function getPrepPlan(organizationId, planId) {
  return authRequest('GET', prepPlanPath(organizationId, planId))
}

// Saves changes made to the version last seen; a 409 means someone else
// changed the plan in the meantime.
export function updatePrepPlan(organizationId, planId, { version, bufferPercent, lines }) {
  return authRequest('PATCH', prepPlanPath(organizationId, planId), {
    version,
    ...(bufferPercent !== undefined ? { buffer_percent: bufferPercent } : {}),
    lines,
  })
}

// action: 'refresh', 'finalize', or 'reopen'.
export function prepPlanAction(organizationId, planId, action, version) {
  return authRequest('POST', prepPlanPath(organizationId, planId, `${action}/`), { version })
}

export function deletePrepPlan(organizationId, planId) {
  return authRequest('DELETE', prepPlanPath(organizationId, planId))
}
