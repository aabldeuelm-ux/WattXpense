const BASE = '/api'

function readCookie(name) {
  return document.cookie
    .split('; ')
    .map((part) => part.split('='))
    .find(([key]) => key === name)?.[1]
}

async function request(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const headers = { Accept: 'application/json', ...(options.headers || {}) }
  let body = options.body

  if (body !== undefined && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
    body = typeof body === 'string' ? body : JSON.stringify(body)
  }

  // Cookie-authenticated writes must echo the readable CSRF cookie. The session cookie
  // itself stays HttpOnly, so this is the only token JavaScript can see.
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = readCookie('wattxpense_csrf')
    if (csrf) headers['X-CSRF-Token'] = decodeURIComponent(csrf)
  }

  const response = await fetch(`${BASE}${path}`, {
    credentials: 'same-origin',
    ...options,
    method,
    headers,
    body,
  })

  const text = await response.text()
  let payload = null
  try {
    payload = text ? JSON.parse(text) : null
  } catch {
    payload = null
  }

  if (!response.ok) {
    const detail = payload?.detail
    const message = Array.isArray(detail)
      ? detail.map((item) => item.msg || item).join('; ')
      : detail || `Request failed (${response.status})`
    const error = new Error(message)
    error.status = response.status
    // A session that expired mid-visit sends the app back to the sign-in screen.
    if (response.status === 401 && !path.startsWith('/auth/')) {
      window.dispatchEvent(new CustomEvent('wattxpense:unauthorized'))
    }
    throw error
  }
  return payload
}

const query = (params) => {
  const search = new URLSearchParams()
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, value)
  })
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

export const api = {
  authStatus: () => request('/auth/status'),
  register: (payload) => request('/auth/register', { method: 'POST', body: payload }),
  login: (payload) => request('/auth/login', { method: 'POST', body: payload }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  me: () => request('/auth/me'),
  changePassword: (payload) => request('/auth/password', { method: 'POST', body: payload }),

  meta: () => request('/meta'),
  health: () => request('/health'),

  devices: () => request('/devices'),
  createDevice: (payload) => request('/devices', { method: 'POST', body: payload }),
  rotateDeviceToken: (id) => request(`/devices/${encodeURIComponent(id)}/token`, { method: 'POST' }),
  relay: (id) => request(`/devices/${encodeURIComponent(id)}/relay`),
  commandRelay: (id, state, confirm = true) =>
    request(`/devices/${encodeURIComponent(id)}/relay`, {
      method: 'POST',
      body: { state, confirm },
    }),

  summary: (deviceId) => request(`/analytics/summary${query({ device_id: deviceId })}`),
  usage: (view, deviceId) => request(`/analytics/usage${query({ view, device_id: deviceId })}`),
  readings: (deviceId, limit = 50) => request(`/readings${query({ device_id: deviceId, limit })}`),

  tariff: () => request('/billing/tariff'),
  tariffs: () => request('/billing/tariffs'),
  setTariff: (payload) => request('/billing/tariff', { method: 'POST', body: payload }),
  preview: (energyKwh, days, peakShare) =>
    request(`/billing/preview${query({ energy_kwh: energyKwh, days, peak_share: peakShare })}`),

  forecast: (horizonDays, deviceId) =>
    request(`/ai/forecast${query({ horizon_days: horizonDays, device_id: deviceId })}`),
  anomalies: (deviceId) => request(`/ai/anomalies${query({ device_id: deviceId })}`),
  insights: (deviceId) => request(`/ai/insights${query({ device_id: deviceId })}`),
  aiStatus: () => request('/ai/status'),
  explain: (payload) => request('/ai/explain', { method: 'POST', body: payload || {} }),
}

export default api
