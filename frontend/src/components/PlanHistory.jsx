import { useEffect, useState } from 'react'
import { api } from '../api.js'

const SEG_COLORS = ['#3ecf8e', '#4da3ff', '#f5a623', '#b08cff',
                    '#ff8f5d', '#5dd6d6', '#e35dd6', '#9be05d']

/** 场景的规划方案历史：不可变快照 + 版本 / post-check 过期标记。 */
export default function PlanHistory({ scenarioId, refreshKey, onShowSnapshot }) {
  const [items, setItems] = useState([])
  const [currentVersion, setCurrentVersion] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [openId, setOpenId] = useState(null)
  const [detail, setDetail] = useState(null)

  const load = async () => {
    if (!scenarioId) return
    try {
      const r = await api.listPlans(scenarioId)
      setItems(r.plans)
      setCurrentVersion(r.current_allocation_version)
    } catch (e) { setError(String(e.message || e)) }
  }
  useEffect(() => { load() }, [scenarioId, refreshKey])

  const saveSnapshot = async (mode) => {
    setBusy(mode); setError('')
    try {
      const r = await api.savePlan(scenarioId, { mode, note: '' })
      if (!r.saved) {
        setError(`规划不可行，未保存：${r.message || r.status}`)
      }
      await load()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const remove = async (pid, ev) => {
    ev.stopPropagation()
    await api.deletePlan(scenarioId, pid)
    if (openId === pid) { setOpenId(null); setDetail(null) }
    await load()
  }

  const open = async (pid) => {
    if (openId === pid) { setOpenId(null); setDetail(null); return }
    setOpenId(pid); setDetail(null)
    try {
      const d = await api.getPlan(scenarioId, pid)
      setDetail(d)
      onShowSnapshot?.(d)
    } catch (e) { setError(e.message) }
  }

  if (!scenarioId) {
    return <div className="hint">先保存场景，才能留存带版本的规划方案历史。</div>
  }

  return (
    <div>
      <div className="row">
        <button className="primary" disabled={!!busy}
                onClick={() => saveSnapshot('guard_only')}>
          {busy === 'guard_only' ? '求解中…' : '保存规划（仅保护间隔）'}
        </button>
        <button className="primary" disabled={!!busy}
                onClick={() => saveSnapshot('mask_aware')}>
          {busy === 'mask_aware' ? '求解中…' : '保存规划（掩模感知）'}
        </button>
        <span className="muted">当前分配方案 v{currentVersion}</span>
      </div>
      {error && <div className="err-msg">{error}</div>}
      <div className="hint">
        快照不可变：之后编辑频段/排除窗不会移动旧方案，刷新时用版本与 post-check 标为过期，
        原始位置与生成时结论保留可追溯。
      </div>

      <table className="plan-table" style={{ marginTop: 8 }}>
        <thead>
          <tr><th>#</th><th>模式</th><th>生成于版本</th><th>生成时结论</th><th>状态</th><th></th></tr>
        </thead>
        <tbody>
          {items.length === 0 && (
            <tr><td colSpan={6} className="muted" style={{ textAlign: 'center', padding: 8 }}>
              尚无保存的规划方案
            </td></tr>
          )}
          {items.map((p) => (
            <tr key={p.id} className={p.stale ? 'plan-stale-row' : 'plan-fresh-row'}
                style={{ cursor: 'pointer' }} onClick={() => open(p.id)}>
              <td>{p.id}</td>
              <td>{p.mode === 'mask_aware' ? '掩模感知' : '保护间隔'}</td>
              <td>v{p.allocation_version}
                {p.current_allocation_version !== p.allocation_version &&
                  <span className="muted"> → v{p.current_allocation_version}</span>}
              </td>
              <td className="muted">
                冲突 {p.baseline_counts?.error ?? 0} · 警告 {p.baseline_counts?.warning ?? 0}
                {' '}· 待评估 {p.baseline_counts?.pending ?? 0}
              </td>
              <td>{p.stale == null ? '未刷新'
                  : p.stale ? <span className="stale-badge">已过期</span>
                            : <span className="fresh-badge">有效</span>}</td>
              <td><button className="danger" onClick={(e) => remove(p.id, e)}>删除</button></td>
            </tr>
          ))}
        </tbody>
      </table>

      {openId && detail && (
        <div className="snapshot-detail">
          <div className="row" style={{ marginTop: 8 }}>
            <strong>方案 #{detail.id} 明细</strong>
            <span className="spacer" />
            {detail.stale
              ? <span className="stale-badge">已过期（位置未移动）</span>
              : <span className="fresh-badge">仍有效</span>}
          </div>
          {detail.stale_reasons?.length > 0 && (
            <ul className="stale-reasons">
              {detail.stale_reasons.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          )}
          {detail.boundary_violations?.length > 0 && (
            <div className="hint" style={{ color: 'var(--error)' }}>
              越界载波：{detail.boundary_violations
                .map((v) => `${v.carrier}（${v.occupied_mhz[0]}–${v.occupied_mhz[1]} MHz，${
                  v.reason === 'crosses_exclusion' ? '跨越排除窗'
                  : v.reason === 'carrier_removed' ? '载波已删除' : '越出有效净空'}）`)
                .join('；')}
            </div>
          )}
          <table className="plan-table" style={{ marginTop: 6 }}>
            <thead>
              <tr><th>载波</th><th>中心 MHz</th><th>频带范围</th><th>归属段</th><th>归属小片 MHz</th></tr>
            </thead>
            <tbody>
              {detail.result.assignments.map((a) => (
                <tr key={a.name}>
                  <td>{a.name} <span className="muted">{a.polarization}</span></td>
                  <td>{a.center_mhz.toFixed(3)}</td>
                  <td>{a.low_mhz.toFixed(2)}–{a.high_mhz.toFixed(2)}</td>
                  <td><span style={{
                    color: SEG_COLORS[(a.segment_index ?? 0) % SEG_COLORS.length] }}>
                    S{(a.segment_index ?? 0) + 1} {a.segment_mhz?.[0]}–{a.segment_mhz?.[1]}
                  </span></td>
                  <td className="muted">{a.piece_mhz?.[0]}–{a.piece_mhz?.[1]}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="hint" style={{ marginTop: 6 }}>
            每条载波的落段理由：
          </div>
          <ul className="stale-reasons" style={{ color: 'var(--muted)' }}>
            {detail.result.assignments.map((a) => (
              <li key={a.name}><b>{a.name}</b>：{a.assignment_reason}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
