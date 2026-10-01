const POLS = ['H', 'V', 'LHCP', 'RHCP']

export default function CarrierTable({ carriers, masks, onChange, onAdd, onRemove, disabled }) {
  const update = (i, patch) => onChange(carriers.map((c, j) => (j === i ? { ...c, ...patch } : c)))

  return (
    <div>
      <table className="carriers">
        <thead>
          <tr>
            <th>名称</th><th>中心 MHz</th><th>带宽 MHz</th><th>功率 dBm</th>
            <th>极化</th><th>掩模</th><th></th>
          </tr>
        </thead>
        <tbody>
          {carriers.map((c, i) => (
            <tr key={i}>
              <td><input name="name" value={c.name} disabled={disabled}
                         onChange={(e) => update(i, { name: e.target.value })} /></td>
              <td><input className="num" type="number" step="0.1" value={c.center_mhz} disabled={disabled}
                         onChange={(e) => update(i, { center_mhz: parseFloat(e.target.value) })} /></td>
              <td><input className="num" type="number" step="0.1" min="0.1" value={c.bandwidth_mhz} disabled={disabled}
                         onChange={(e) => update(i, { bandwidth_mhz: parseFloat(e.target.value) })} /></td>
              <td><input className="num" type="number" step="0.5" value={c.power_dbm} disabled={disabled}
                         onChange={(e) => update(i, { power_dbm: parseFloat(e.target.value) })} /></td>
              <td>
                <select value={c.polarization} disabled={disabled}
                        onChange={(e) => update(i, { polarization: e.target.value })}>
                  {POLS.map((p) => <option key={p}>{p}</option>)}
                </select>
              </td>
              <td>
                <select value={c.mask_name} disabled={disabled}
                        onChange={(e) => update(i, { mask_name: e.target.value })}>
                  {masks.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
                </select>
              </td>
              <td className="del">
                <button className="danger" title="删除" disabled={disabled}
                        onClick={() => onRemove(i)}>×</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row" style={{ marginTop: 8 }}>
        <button onClick={onAdd} disabled={disabled}>＋ 添加载波</button>
        <span className="muted">{carriers.length} 个载波</span>
      </div>
    </div>
  )
}
