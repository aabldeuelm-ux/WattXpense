const BASE = '/api'

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const text = await response.text()
  const payload = text ? JSON.parse(text) : null
  if (!response.ok) {
    const detail = payload?.detail
    const message = Array.isArray(detail)
      ? detail.map((item) => item.msg).join('; ')
      : detail || `Request failed (${response.status})`
    const error = new Error(message)
    error.status = response.status
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
  meta: () => request('/meta'),
  health: () => request('/health'),
  devices: () => request('/devices'),
  device: (id) => request('/devices', { method: 'POST', body: JSON.stringify({ id }) }),
  summary: (deviceId) => request(`/analytics/summary${query({ device_id: deviceId })}`),
  usage: (view, deviceId) => request(`/analytics/usage${query({ view, device_id: deviceId })}`),
  readings: (deviceId, limit = 200) => request(`/readings${query({ device_id: deviceId, limit })}`),
  tariff: () => request('/billing/tariff'),
  tariffs: () => request('/billing/tariffs'),
  preview: (energyKwh, days, peakShare) =>
    request(`/billing/preview${query({ energy_kwh: energyKwh, days, peak_share: peakShare })}`),
  forecast: (horizonDays, deviceId) =>
    request(`/ai/forecast${query({ horizon_days: horizonDays, device_id: deviceId })}`),
  anomalies: (deviceId) => request(`/ai/anomalies${query({ device_id: deviceId })}`),
  insights: (deviceId) => request(`/ai/insights${query({ device_id: deviceId })}`),
  aiStatus: () => request('/ai/status'),
  explain: (payload) => request('/ai/explain', { method: 'POST', body: JSON.stringify(payload || {}) }),
}

export default api
