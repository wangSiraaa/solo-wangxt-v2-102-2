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

  // 规划方案快照（不可变历史 + 版本 / post-check 过期标记）
  listPlans: (id) => request(`/scenarios/${id}/plans?refresh=true`),
  getPlan: (id, pid) => request(`/scenarios/${id}/plans/${pid}?refresh=true`),
  savePlan: (id, payload) =>
    request(`/scenarios/${id}/plans`, { method: 'POST', body: JSON.stringify(payload) }),
  deletePlan: (id, pid) =>
    request(`/scenarios/${id}/plans/${pid}`, { method: 'DELETE' }),

  // 导出 / 导入（原子：后端校验失败不写入任何内容）
  exportScenario: (id) => request(`/scenarios/${id}/export`),
  importScenario: (doc) =>
    request('/scenarios/import', { method: 'POST', body: JSON.stringify(doc) }),
}
