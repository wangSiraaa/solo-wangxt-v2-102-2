const POLS = ['H', 'V', 'LHCP', 'RHCP']
const OPTIONS = [
  { v: 'forbidden', label: '禁止', cls: 'forbidden' },
  { v: 'unknown', label: '待评估', cls: 'pending' },
  { v: 'allowed', label: '允许', cls: 'allowed' },
]

export function policyKey(a, b) {
  return [a, b].sort().join('|')
}

/** 异极化对复用规则矩阵（未知隔离度默认“待评估”）。 */
export default function RulesPanel({ rules, onChange, disabled }) {
  const set = (patch) => onChange({ ...rules, ...patch })
  const setPolicy = (a, b, v) =>
    set({ reuse_policy: { ...rules.reuse_policy, [policyKey(a, b)]: v } })

  const pairs = []
  for (let i = 0; i < POLS.length; i++)
    for (let j = i + 1; j < POLS.length; j++) pairs.push([POLS[i], POLS[j]])

  return (
    <div>
      <div className="row" style={{ marginBottom: 8 }}>
        <label className="field-label">保护间隔</label>
        <input className="field" type="number" step="0.1" min="0" style={{ width: 80 }}
               value={rules.guard_required_mhz} disabled={disabled}
               onChange={(e) => set({ guard_required_mhz: parseFloat(e.target.value) })} />
        <span className="muted">MHz</span>
        <label className="field-label" style={{ marginLeft: 10 }}>尾部泄漏限值</label>
        <input className="field" type="number" step="1" style={{ width: 80 }}
               value={rules.leakage_limit_dbm} disabled={disabled}
               onChange={(e) => set({ leakage_limit_dbm: parseFloat(e.target.value) })} />
        <span className="muted">dBm</span>
      </div>
      <div className="policy-grid">
        <span className="muted">异极化对同频复用规则</span>
        <span className="head">禁止</span><span className="head">待评估</span><span className="head">允许</span>
        {pairs.map(([a, b]) => {
          const cur = rules.reuse_policy[policyKey(a, b)] || 'unknown'
          return (
            <div key={a + b} className="row" style={{ gridColumn: '1 / -1' }}>
              <span style={{ width: 92 }}>{a} ↔ {b}</span>
              <span className="seg">
                {OPTIONS.map((o) => (
                  <button key={o.v} className={`${cur === o.v ? 'on ' + o.cls : ''}`}
                          disabled={disabled}
                          onClick={() => setPolicy(a, b, o.v)}>{o.label}</button>
                ))}
              </span>
            </div>
          )
        })}
      </div>
      <div className="hint">
        待评估：隔离度未知，同频段重叠不下违规定性；允许：已知隔离度足够，可同频复用；
        禁止 / 同极化对：必须按保护间隔排开。
      </div>
    </div>
  )
}
