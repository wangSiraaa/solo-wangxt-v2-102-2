const TYPE_LABEL = {
  overlap: '频带重叠',
  guard_shortfall: '保护带不足',
  mask_tail: '掩模尾部越界',
  reuse_unknown: '复用待评估',
}
const SEV_LABEL = { error: '冲突', warning: '警告', pending: '待评估' }

export default function FindingsList({ findings, selectedPair, onSelect }) {
  if (!findings?.length) {
    return <div style={{ color: 'var(--ok)' }}>未发现冲突，频带配置满足规则。</div>
  }
  return (
    <ul className="findings">
      {findings.map((f, i) => {
        const key = [f.carrier_a, f.carrier_b].sort().join('|')
        const selKey = selectedPair ? [...selectedPair].sort().join('|') : null
        const meta = []
        if (f.type === 'mask_tail') {
          meta.push(`泄漏 ${f.leakage_dbm} dBm > 限值 ${f.limit_dbm} dBm（超出 ${f.excess_dbm} dB）`)
          meta.push(`方向 ${f.direction}`)
        } else if (f.type === 'guard_shortfall') {
          meta.push(`净距 ${f.gap_mhz} MHz < 要求 ${f.required_mhz} MHz（差 ${f.deficit_mhz} MHz）`)
        } else if (f.overlap_mhz != null) {
          meta.push(`重叠 ${f.overlap_mhz} MHz`)
        }
        if (f.polarization_pair) {
          meta.push(`极化对 ${f.polarization_pair}（规则: ${f.reuse_policy}）`)
        }
        return (
          <li key={i} className={`${f.severity} ${selKey === key ? 'flash' : ''}`}
              onClick={() => onSelect([f.carrier_a, f.carrier_b])}>
            <span className="tag">{SEV_LABEL[f.severity]}</span>
            <span className="pair">{f.carrier_a} ⇄ {f.carrier_b}</span>
            <span className="muted"> · {TYPE_LABEL[f.type] || f.type}</span>
            <div>{f.message}</div>
            {meta.map((m, k) => <span key={k} className="meta">{m}</span>)}
          </li>
        )
      })}
    </ul>
  )
}
