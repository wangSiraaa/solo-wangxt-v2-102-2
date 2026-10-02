/** 频谱分配方案编辑：多个可用频段（段）+ 不能跨越的排除窗 + 版本号。 */
export default function AllocationPanel({ allocation, onChange, disabled, migrated }) {
  const segs = allocation.segments || []
  const excs = allocation.exclusions || []

  const update = (patch) => onChange({ ...allocation, ...patch })

  const setSeg = (i, patch) =>
    update({ segments: segs.map((s, j) => (j === i ? { ...s, ...patch } : s)) })
  const setExc = (i, patch) =>
    update({ exclusions: excs.map((e, j) => (j === i ? { ...e, ...patch } : e)) })

  return (
    <div>
      <div className="row" style={{ marginBottom: 6 }}>
        <label className="field-label">方案名</label>
        <input className="field" style={{ flex: 1 }} value={allocation.name || ''}
               disabled={disabled}
               onChange={(e) => update({ name: e.target.value })} />
        <span className="alloc-version" title="每次修改段或排除窗，保存后版本自动 +1；旧规划据此标为过期">
          v{allocation.version ?? 1}
        </span>
        {migrated && <span className="alloc-migrated" title="旧场景只有单一可用范围，已迁移为等价单段">
          旧版单频段已迁移
        </span>}
      </div>

      <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>
        可用频段（载波占用必须完整落在某一段内，段间空洞不可跨越）
      </div>
      <table className="alloc-table">
        <tbody>
          {segs.map((s, i) => (
            <tr key={i}>
              <td className="seg-idx">S{i + 1}</td>
              <td><input className="num field" type="number" step="0.1" value={s.low_mhz}
                         disabled={disabled}
                         onChange={(e) => setSeg(i, { low_mhz: parseFloat(e.target.value) })} /></td>
              <td className="muted">–</td>
              <td><input className="num field" type="number" step="0.1" value={s.high_mhz}
                         disabled={disabled}
                         onChange={(e) => setSeg(i, { high_mhz: parseFloat(e.target.value) })} /></td>
              <td className="muted">MHz</td>
              <td className="del">
                <button className="danger" title="删除该段" disabled={disabled || segs.length <= 1}
                        onClick={() => update({ segments: segs.filter((_, j) => j !== i) })}>×</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button disabled={disabled} onClick={() => update({
        segments: [...segs, { low_mhz: segs.length ? Math.max(...segs.map((s) => s.high_mhz)) + 10 : 80,
                              high_mhz: segs.length ? Math.max(...segs.map((s) => s.high_mhz)) + 20 : 100 }],
      })}>＋ 添加可用段</button>

      <div className="muted" style={{ fontSize: 11, margin: '10px 0 4px' }}>
        排除窗 / 保护空洞（载波不能跨越；段内排除窗会把该段切成两片）
      </div>
      <table className="alloc-table">
        <tbody>
          {excs.map((e, i) => (
            <tr key={i}>
              <td className="seg-idx exc-idx">E{i + 1}</td>
              <td><input className="num field" type="number" step="0.1" value={e.low_mhz}
                         disabled={disabled}
                         onChange={(ev) => setExc(i, { low_mhz: parseFloat(ev.target.value) })} /></td>
              <td className="muted">–</td>
              <td><input className="num field" type="number" step="0.1" value={e.high_mhz}
                         disabled={disabled}
                         onChange={(ev) => setExc(i, { high_mhz: parseFloat(ev.target.value) })} /></td>
              <td><input className="field" style={{ width: 90 }} placeholder="原因（可选）"
                         value={e.reason || ''} disabled={disabled}
                         onChange={(ev) => setExc(i, { reason: ev.target.value })} /></td>
              <td className="del">
                <button className="danger" title="删除排除窗" disabled={disabled}
                        onClick={() => update({ exclusions: excs.filter((_, j) => j !== i) })}>×</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button disabled={disabled} onClick={() => update({
        exclusions: [...excs, { low_mhz: 100, high_mhz: 102, reason: '' }],
      })}>＋ 添加排除窗</button>
      <div className="hint">
        段重叠、排除窗悬空或把全部可用段清空的分配无法保存（整体拒绝，不会部分生效）；
        仅编辑方案名不改变版本。
      </div>
    </div>
  )
}
