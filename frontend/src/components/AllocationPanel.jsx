/** 频谱分配方案编辑器：多段可用频段 + 排除窗（保存时整体校验，修改生成新版本）。 */
export default function AllocationPanel({ allocation, savedVersion, onChange, onSave, disabled }) {
  const set = (patch) => onChange({ ...allocation, ...patch })

  const setSeg = (i, patch) =>
    set({ segments: allocation.segments.map((s, j) => (j === i ? { ...s, ...patch } : s)) })
  const setExc = (i, patch) =>
    set({ exclusions: allocation.exclusions.map((e, j) => (j === i ? { ...e, ...patch } : e)) })

  const num = (v) => (v === '' ? '' : parseFloat(v))

  return (
    <div>
      <div className="row" style={{ marginBottom: 6 }}>
        <span className="muted">可用频段（可不连续）</span>
        <span className="spacer" />
        {savedVersion != null && <span className="version-pill">当前 v{savedVersion}</span>}
      </div>
      <table className="carriers alloc-table">
        <thead>
          <tr><th>标签</th><th>下限 MHz</th><th>上限 MHz</th><th></th></tr>
        </thead>
        <tbody>
          {allocation.segments.map((s, i) => (
            <tr key={i}>
              <td><input value={s.label} disabled={disabled} placeholder={`S${i + 1}`}
                         onChange={(e) => setSeg(i, { label: e.target.value })} /></td>
              <td><input className="num" type="number" step="0.1" value={s.low_mhz} disabled={disabled}
                         onChange={(e) => setSeg(i, { low_mhz: num(e.target.value) })} /></td>
              <td><input className="num" type="number" step="0.1" value={s.high_mhz} disabled={disabled}
                         onChange={(e) => setSeg(i, { high_mhz: num(e.target.value) })} /></td>
              <td className="del">
                <button className="danger" disabled={disabled || allocation.segments.length <= 1}
                        onClick={() => set({ segments: allocation.segments.filter((_, j) => j !== i) })}>×</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row" style={{ margin: '6px 0 10px' }}>
        <button disabled={disabled}
                onClick={() => set({ segments: [...allocation.segments, { low_mhz: 0, high_mhz: 0, label: '' }] })}>
          ＋ 添加频段
        </button>
      </div>

      <div className="muted" style={{ marginBottom: 6 }}>排除窗（保护空洞，载波不得跨越）</div>
      <table className="carriers alloc-table">
        <thead>
          <tr><th>原因</th><th>下限 MHz</th><th>上限 MHz</th><th></th></tr>
        </thead>
        <tbody>
          {allocation.exclusions.length === 0 && (
            <tr><td colSpan={4} className="muted">无排除窗</td></tr>
          )}
          {allocation.exclusions.map((x, i) => (
            <tr key={i}>
              <td><input value={x.reason} disabled={disabled} placeholder="如：预留保护空洞"
                         onChange={(e) => setExc(i, { reason: e.target.value })} /></td>
              <td><input className="num" type="number" step="0.1" value={x.low_mhz} disabled={disabled}
                         onChange={(e) => setExc(i, { low_mhz: num(e.target.value) })} /></td>
              <td><input className="num" type="number" step="0.1" value={x.high_mhz} disabled={disabled}
                         onChange={(e) => setExc(i, { high_mhz: num(e.target.value) })} /></td>
              <td className="del">
                <button className="danger" disabled={disabled}
                        onClick={() => set({ exclusions: allocation.exclusions.filter((_, j) => j !== i) })}>×</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row" style={{ marginTop: 6 }}>
        <button disabled={disabled}
                onClick={() => set({ exclusions: [...allocation.exclusions, { low_mhz: 0, high_mhz: 0, reason: '' }] })}>
          ＋ 添加排除窗
        </button>
        <span className="spacer" />
        {onSave && (
          <button className="primary" disabled={disabled} onClick={onSave}
                  title="整体校验通过后保存为新版本；旧规划记录按版本标记过期">
            保存方案新版本
          </button>
        )}
      </div>
      <div className="hint">
        载波占用带宽必须完整落在某一可用段内，且不得跨越段间空洞或排除窗；
        保存修改会生成新版本，旧规划记录不会被改动，只会被标记为过期。
      </div>
    </div>
  )
}
