const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  health: () => request('/health'),
  masks: () => request('/masks'),
  listScenarios: () => request('/scenarios'),
  getScenario: (id) => request(`/scenarios/${id}`),
  createScenario: (payload) =>
    request('/scenarios', { method: 'POST', body: JSON.stringify(payload) }),
  updateScenario: (id, payload) =>
    request(`/scenarios/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteScenario: (id) =>
    request(`/scenarios/${id}`, { method: 'DELETE' }),
  analyze: (payload) =>
    request('/analyze', { method: 'POST', body: JSON.stringify(payload) }),
  plan: (payload) =>
    request('/plan', { method: 'POST', body: JSON.stringify(payload) }),
  // 频谱分配方案（版本化）
  getAllocation: (id) => request(`/scenarios/${id}/allocation`),
  getAllocationVersions: (id) => request(`/scenarios/${id}/allocation/versions`),
  putAllocation: (id, payload) =>
    request(`/scenarios/${id}/allocation`, { method: 'PUT', body: JSON.stringify(payload) }),
  // 规划记录（冻结保存 + 过期标记）
  listPlans: (id) => request(`/scenarios/${id}/plans`),
  savePlan: (id, payload) =>
    request(`/scenarios/${id}/plans`, { method: 'POST', body: JSON.stringify(payload) }),
  // 导出 / 导入
  exportScenario: (id) => request(`/scenarios/${id}/export`),
  importScenario: (payload) =>
    request('/scenarios/import', { method: 'POST', body: JSON.stringify(payload) }),
}
