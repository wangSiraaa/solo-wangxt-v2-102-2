import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import CarrierTable from './components/CarrierTable.jsx'
import RulesPanel, { policyKey } from './components/RulesPanel.jsx'
import AllocationPanel from './components/AllocationPanel.jsx'
import FindingsList from './components/FindingsList.jsx'
import PowerSummary from './components/PowerSummary.jsx'
import BandChart from './components/BandChart.jsx'
import SpectrumChart from './components/SpectrumChart.jsx'
import MaskPreview from './components/MaskPreview.jsx'

const EMPTY_RULES = { guard_required_mhz: 1.0, leakage_limit_dbm: -45.0, reuse_policy: {} }
const DEFAULT_ALLOCATION = {
  version: null,
  segments: [{ low_mhz: 80, high_mhz: 220, label: 'S1' }],
  exclusions: [],
}

const newCarrier = (i) => ({
  name: `C${i + 1}`, center_mhz: 100 + i * 6, bandwidth_mhz: 4,
  power_dbm: 20, polarization: 'H', mask_name: 'strict',
})

/** 提交给后端的几何（数值化），不含版本信息。 */
function allocPayload(a) {
  return {
    segments: a.segments.map((s) => ({ low_mhz: Number(s.low_mhz), high_mhz: Number(s.high_mhz), label: s.label || '' })),
    exclusions: a.exclusions.map((e) => ({ low_mhz: Number(e.low_mhz), high_mhz: Number(e.high_mhz), reason: e.reason || '' })),
  }
}

/** 规划结果所基于的方案几何是否与当前编辑的一致（不一致提示重新求解）。 */
function sameGeometry(planAlloc, current) {
  if (!planAlloc) return true
  const norm = (x) => JSON.stringify({
    s: (x.segments || []).map((s) => [s.low_mhz, s.high_mhz, s.label || '']).sort(),
    e: (x.exclusions || []).map((e) => [e.low_mhz, e.high_mhz, e.reason || '']).sort(),
  })
  return norm(planAlloc) === norm(allocPayload(current))
}

export default function App() {
  const [masks, setMasks] = useState([])
  const [carriers, setCarriers] = useState([newCarrier(0)])
  const [rules, setRules] = useState(EMPTY_RULES)
  const [allocation, setAllocation] = useState(DEFAULT_ALLOCATION)
  const [scenarios, setScenarios] = useState([])
  const [scenarioId, setScenarioId] = useState(null)
  const [scenarioName, setScenarioName] = useState('未命名场景')
  const [savedPlans, setSavedPlans] = useState([])
  const [analysis, setAnalysis] = useState(null)
  const [plan, setPlan] = useState(null)
  const [planMode, setPlanMode] = useState('guard_only')
  const [planView, setPlanView] = useState(false)
  const [tab, setTab] = useState('spectrum')
  const [selectedPair, setSelectedPair] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    api.masks().then(setMasks).catch((e) => setError(String(e)))
    refreshScenarios()
  }, [])

  const refreshScenarios = () =>
    api.listScenarios().then(setScenarios).catch(() => {})

  const refreshPlans = useCallback((sid) => {
    if (!sid) { setSavedPlans([]); return }
    api.listPlans(sid).then(setSavedPlans).catch(() => setSavedPlans([]))
  }, [])

  const runAnalyze = useCallback(async () => {
    setBusy('analyze'); setError(''); setPlan(null)
    try {
      const res = await api.analyze({
        carriers,
        rules: { ...rules, reuse_policy: normalizePolicy(rules.reuse_policy) },
        plot_grid_mhz: 0.05,
        allocation: allocPayload(allocation),
      })
      setAnalysis(res)
      setTab('spectrum')
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }, [carriers, rules, allocation])

  const runPlan = useCallback(async () => {
    setBusy('plan'); setError('')
    try {
      const res = await api.plan({
        carriers,
        rules: { ...rules, reuse_policy: normalizePolicy(rules.reuse_policy) },
        mode: planMode,
        allocation: allocPayload(allocation),
      })
      setPlan(res)
      setPlanView(false) // 默认显示原始（冲突）谱；可切换到规划后
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }, [carriers, rules, allocation, planMode])

  const loadScenario = async (id) => {
    if (!id) { setScenarioId(null); setSavedPlans([]); return }
    setBusy('load'); setError('')
    try {
      const sc = await api.getScenario(id)
      setScenarioId(sc.id); setScenarioName(sc.name)
      setCarriers(sc.carriers.map(({ id, ...c }) => c))
      setRules({ guard_required_mhz: sc.guard_required_mhz,
                 leakage_limit_dbm: sc.leakage_limit_dbm, reuse_policy: sc.reuse_policy || {} })
      setAllocation(sc.allocation
        ? { version: sc.allocation.version, segments: sc.allocation.segments,
            exclusions: sc.allocation.exclusions }
        : DEFAULT_ALLOCATION)
      setAnalysis(null); setPlan(null); setSelectedPair(null)
      refreshPlans(sc.id)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const saveScenario = async () => {
    setBusy('save'); setError('')
    const payload = {
      name: scenarioName, description: '',
      guard_required_mhz: rules.guard_required_mhz, leakage_limit_dbm: rules.leakage_limit_dbm,
      reuse_policy: normalizePolicy(rules.reuse_policy), carriers,
      allocation: allocPayload(allocation),
    }
    try {
      const saved = scenarioId
        ? await api.updateScenario(scenarioId, payload)
        : await api.createScenario(payload)
      setScenarioId(saved.id)
      if (saved.allocation) {
        setAllocation({ version: saved.allocation.version,
                        segments: saved.allocation.segments,
                        exclusions: saved.allocation.exclusions })
      }
      await refreshScenarios()
      refreshPlans(saved.id)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const saveAllocationVersion = async () => {
    if (!scenarioId) { setError('请先保存场景，再保存分配方案新版本'); return }
    setBusy('alloc'); setError('')
    try {
      const saved = await api.putAllocation(scenarioId, allocPayload(allocation))
      setAllocation({ version: saved.version, segments: saved.segments,
                      exclusions: saved.exclusions })
      refreshPlans(scenarioId) // 旧规划记录按版本/几何重新判定过期
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const savePlanRecord = async () => {
    if (!scenarioId || !plan?.feasible) return
    setBusy('saveplan'); setError('')
    try {
      await api.savePlan(scenarioId, { mode: planMode, assignments: plan.assignments })
      refreshPlans(scenarioId)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const exportScenario = async () => {
    if (!scenarioId) return
    setBusy('export'); setError('')
    try {
      const data = await api.exportScenario(scenarioId)
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `${scenarioName || 'scenario'}.spectrum.json`
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const importScenario = async (file) => {
    if (!file) return
    setBusy('import'); setError('')
    try {
      const text = await file.text()
      const saved = await api.importScenario(JSON.parse(text))
      await refreshScenarios()
      await loadScenario(saved.id)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const deleteScenario = async () => {
    if (!scenarioId) return
    setBusy('del'); setError('')
    try {
      await api.deleteScenario(scenarioId)
      setScenarioId(null)
      setSavedPlans([])
      await refreshScenarios()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const status = analysis?.status
  // 频段图始终显示录入频带（按原始冲突着色），规划位置以绿色描边框叠加
  const shownBands = analysis?.bands
  const shownFindings = analysis?.findings || []
  const plannedSpectrum = plan?.feasible ? plan.spectrum : null
  const plannedBands = plan?.feasible ? plan.bands : null
  const shownSpectrum = planView ? plannedSpectrum : analysis?.spectrum
  const spectrumBands = planView ? plannedBands : analysis?.bands
  const shownFindingsList = planView ? plan?.post_check?.findings : shownFindings
  const shownPower = planView ? plan?.post_check?.power_summary : analysis?.power_summary
  const shownStatus = planView ? plan?.post_check?.status : status
  const planGeometryStale = plan?.feasible && !sameGeometry(plan.allocation, allocation)

  return (
    <>
      <header className="app-header">
        <h1>📡 频谱工作台</h1>
        <span className="badge-offline">离线简化模型 · 不连接设备 · 不生成发射指令</span>
        <span className="spacer" />
        {shownStatus && (
          <span className={`status-pill ${shownStatus}`}>
            {shownStatus === 'ok' ? '满足规则' : shownStatus === 'conflict' ? '存在冲突' : '需要关注'}
          </span>
        )}
      </header>

      <div className="layout">
        {/* 左列：录入与规则 */}
        <div>
          <div className="panel">
            <h2>场景（PostgreSQL）</h2>
            <div className="row">
              <select className="field" style={{ flex: 1 }}
                      value={scenarioId ?? ''} onChange={(e) => loadScenario(e.target.value ? Number(e.target.value) : null)}>
                <option value="">— 未保存的编辑 —</option>
                {scenarios.map((s) => <option key={s.id} value={s.id}>{s.name}（{s.carrier_count}）</option>)}
              </select>
            </div>
            <div className="row" style={{ marginTop: 8 }}>
              <input className="field" style={{ flex: 1 }} value={scenarioName}
                     onChange={(e) => setScenarioName(e.target.value)} placeholder="场景名" />
              <button className="primary" onClick={saveScenario} disabled={!!busy}>
                {scenarioId ? '更新' : '保存'}
              </button>
              {scenarioId && <button className="danger" onClick={deleteScenario} disabled={!!busy}>删除</button>}
            </div>
            <div className="row" style={{ marginTop: 8 }}>
              <button onClick={exportScenario} disabled={!!busy || !scenarioId}>导出 JSON</button>
              <label className={`button-like ${busy ? 'disabled' : ''}`}>
                导入 JSON
                <input type="file" accept=".json,application/json" style={{ display: 'none' }}
                       disabled={!!busy}
                       onChange={(e) => { importScenario(e.target.files?.[0]); e.target.value = '' }} />
              </label>
            </div>
          </div>

          <div className="panel">
            <h2>载波录入</h2>
            <CarrierTable carriers={carriers} masks={masks} onChange={setCarriers}
                          onAdd={() => setCarriers([...carriers, newCarrier(carriers.length)])}
                          onRemove={(i) => setCarriers(carriers.filter((_, j) => j !== i))}
                          disabled={!!busy} />
          </div>

          <div className="panel">
            <h2>频谱分配方案（可用频段 + 排除窗）</h2>
            <AllocationPanel allocation={allocation} savedVersion={allocation.version}
                             onChange={(a) => setAllocation({ ...a, version: allocation.version })}
                             onSave={scenarioId ? saveAllocationVersion : null}
                             disabled={!!busy} />
          </div>

          <div className="panel">
            <h2>规则与极化复用</h2>
            <RulesPanel rules={rules} onChange={setRules} disabled={!!busy} />
          </div>

          <div className="panel">
            <div className="row">
              <button className="primary" onClick={runAnalyze} disabled={!!busy || !carriers.length}>
                {busy === 'analyze' ? '计算中…' : '▶ 检查冲突 / 绘制频段'}
              </button>
            </div>
            {error && <div className="err-msg">{error}</div>}
            <div className="hint">检查：频带重叠 · 保护带不足 · 掩模尾部越界（定向到载波对）· 越出可用空闲窗；功率在线性域汇总。</div>
          </div>
        </div>

        {/* 右列：结果 */}
        <div>
          <div className="panel">
            <div className="tabs">
              <button className={tab === 'spectrum' ? 'on' : ''} onClick={() => setTab('spectrum')}>频段与发射谱</button>
              <button className={tab === 'masks' ? 'on' : ''} onClick={() => setTab('masks')}>掩模库</button>
            </div>

            {tab === 'spectrum' && (
              <>
                <BandChart bands={shownBands} findings={shownFindings} plan={plan}
                           allocation={allocation}
                           selectedPair={selectedPair}
                           onPick={(name) => setSelectedPair(
                             selectedPair && selectedPair.includes(name) && selectedPair.length === 2
                               ? null
                               : selectedPair
                                 ? [selectedPair[0], name]
                                 : [name])} />
                <div className="row" style={{ marginBottom: 4 }}>
                  {plan?.feasible && (
                    <span className="seg">
                      <button className={!planView ? 'on' : ''} onClick={() => setPlanView(false)}>
                        录入频带（冲突着色）
                      </button>
                      <button className={planView ? 'on allowed' : ''} onClick={() => setPlanView(true)}>
                        规划后频带（复核 {plan.post_check?.counts.error}/{plan.post_check?.counts.warning}/{plan.post_check?.counts.pending}）
                      </button>
                    </span>
                  )}
                </div>
                <SpectrumChart spectrum={shownSpectrum} bands={spectrumBands} allocation={allocation} />
                <div className="plot-note">
                  提示：蓝虚线为可用段边界、红区为排除窗；点击频段条选择载波，点击冲突条目高亮载波对。
                </div>
              </>
            )}
            {tab === 'masks' && <MaskPreview masks={masks} />}
          </div>

          <div className="panel">
            <h2>OR-Tools 频率规划</h2>
            <div className="row">
              <span className="muted">
                放置范围：{allocation.segments.length} 段可用频段
                {allocation.exclusions.length > 0 && ` · ${allocation.exclusions.length} 个排除窗`}
                {allocation.version != null && ` · 方案 v${allocation.version}`}
              </span>
              <span className="seg">
                <button className={planMode === 'guard_only' ? 'on' : ''}
                        onClick={() => setPlanMode('guard_only')}>仅保护间隔</button>
                <button className={planMode === 'mask_aware' ? 'on' : ''}
                        onClick={() => setPlanMode('mask_aware')}>掩模感知</button>
              </span>
              <button className="primary" onClick={runPlan} disabled={!!busy || !carriers.length}>
                {busy === 'plan' ? '求解中…' : '求解频率位置'}
              </button>
              {plan?.feasible && scenarioId && !planGeometryStale && (
                <button onClick={savePlanRecord} disabled={!!busy}>
                  {busy === 'saveplan' ? '保存中…' : '冻结保存为规划记录'}
                </button>
              )}
            </div>
            <div className="hint">
              每个载波被分配到一个能完整容纳其带宽的空闲窗；目标：在 1 kHz 网格上最小化相对录入位置的总偏移；
              掩模感知模式按双向尾部泄漏达标反算间隔（含 0.5 dB 裕量）。
            </div>
            {planGeometryStale && (
              <div className="err-msg">⚠ 分配方案已修改，当前规划结果基于旧几何，请重新求解后再保存。</div>
            )}
            {plan && <PlanResult plan={plan} />}
          </div>

          {savedPlans.length > 0 && (
            <div className="panel">
              <h2>规划记录（冻结保存 · 过期自动标记）</h2>
              <SavedPlans plans={savedPlans} />
            </div>
          )}

          <div className="panel">
            <h2>冲突定位{planView ? '（规划后复核）' : ''}</h2>
            <FindingsList findings={shownFindingsList} selectedPair={selectedPair}
                          onSelect={(p) => setSelectedPair(
                            JSON.stringify(selectedPair) === JSON.stringify(p) ? null : p)} />
          </div>

          <div className="panel">
            <h2>功率汇总{planView ? '（规划后）' : ''}</h2>
            <PowerSummary summary={shownPower} />
          </div>
        </div>
      </div>
    </>
  )
}

function PlanResult({ plan }) {
  const [open, setOpen] = useState(true)
  if (!plan.feasible) {
    return (
      <div className="err-msg" style={{ marginTop: 8 }}>
        ✗ {plan.status}：{plan.message}
        {plan.infeasible_carriers?.map((d) => (
          <div key={d.name} className="meta">· {d.reason}</div>
        ))}
      </div>
    )
  }
  const counts = plan.post_check?.counts
  const allocOk = plan.post_check?.allocation_check?.all_inside
  return (
    <div style={{ marginTop: 10 }}>
      <div className="row">
        <span style={{ color: 'var(--ok)' }}>✓ {plan.message}</span>
        <span className="spacer" />
        {counts && (
          <span className="muted">
            规划后复核：冲突 {counts.error} · 警告 {counts.warning} · 待评估 {counts.pending}
            {allocOk != null && (allocOk ? ' · 无跨洞占用' : ' · 存在越界！')}
          </span>
        )}
        <button onClick={() => setOpen(!open)}>{open ? '收起' : '展开'}</button>
      </div>
      {open && (
        <table className="plan-table" style={{ marginTop: 8 }}>
          <thead>
            <tr><th>载波</th><th>原中心</th><th>新中心 MHz</th><th>频带范围</th><th>归属段</th><th>偏移 MHz</th></tr>
          </thead>
          <tbody>
            {plan.assignments.map((a) => (
              <tr key={a.name} title={a.segment_reason}>
                <td>{a.name} <span className="muted">{a.polarization}</span></td>
                <td>{a.original_center_mhz.toFixed(3)}</td>
                <td>{a.center_mhz.toFixed(3)}</td>
                <td>{a.low_mhz.toFixed(2)}–{a.high_mhz.toFixed(2)}</td>
                <td><span className="seg-pill">{a.segment_label}</span></td>
                <td className={a.shift_mhz > 0 ? 'shift-pos' : a.shift_mhz < 0 ? 'shift-neg' : ''}>
                  {a.shift_mhz > 0 ? '+' : ''}{a.shift_mhz.toFixed(3)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && (
        <div className="hint" style={{ marginTop: 6 }}>
          {plan.assignments.map((a) => (
            <div key={a.name}>· {a.name}：{a.segment_reason}</div>
          ))}
        </div>
      )}
    </div>
  )
}

function SavedPlans({ plans }) {
  return (
    <ul className="findings">
      {plans.map((p) => (
        <li key={p.id} className={p.stale ? 'error' : 'ok'} style={{ cursor: 'default' }}>
          <span className="tag">{p.stale ? '已过期' : '有效'}</span>
          <span className="pair">方案 v{p.scheme_version} · {p.mode}</span>
          <span className="muted"> · {p.created_at?.slice(0, 19).replace('T', ' ')}</span>
          <div>{p.message}</div>
          {p.post_check?.counts && (
            <span className="meta">
              保存时复核：冲突 {p.post_check.counts.error} · 警告 {p.post_check.counts.warning} · 待评估 {p.post_check.counts.pending}
              {p.post_check.allocation_check && (p.post_check.allocation_check.all_inside ? ' · 无跨洞占用' : ' · 越界')}
            </span>
          )}
          {p.stale && p.stale_reasons.map((m, k) => <span key={k} className="meta">⚠ {m}</span>)}
          {!p.stale && p.scheme_version === p.current_version && (
            <span className="meta">与当前方案 v{p.current_version} 一致</span>
          )}
        </li>
      ))}
    </ul>
  )
}

/** 仅把用户显式设置的规则送给后端；未设置的极化对由后端按“待评估”处理。 */
function normalizePolicy(p) {
  const out = {}
  for (const [k, v] of Object.entries(p || {})) {
    const [a, b] = k.split('|')
    out[policyKey(a, b)] = v
  }
  return out
}
