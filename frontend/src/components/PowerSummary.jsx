export default function PowerSummary({ summary }) {
  if (!summary) return null
  return (
    <div className="power-box">
      <div>
        <div className="muted">总功率（线性 W 求和 → dBm）</div>
        <div className="big">{summary.total_power_dbm.toFixed(2)} dBm</div>
        <div className="muted">{summary.total_power_w.toFixed(4)} W · {summary.carrier_count} 个载波</div>
      </div>
      <div>
        <div className="muted">常见错误：直接对 dBm 求和</div>
        <div className="wrong">{summary.naive_dbm_sum} dBm</div>
        <div className="hint">dB 是对数尺度，必须先换算 W 相加：P = 10·log₁₀(Σ10^(pᵢ/10))</div>
      </div>
    </div>
  )
}
