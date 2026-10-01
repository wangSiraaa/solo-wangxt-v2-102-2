import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import CarrierTable from './components/CarrierTable.jsx'
import RulesPanel, { policyKey } from './components/RulesPanel.jsx'
import FindingsList from './components/FindingsList.jsx'
import PowerSummary from './components/PowerSummary.jsx'
import BandChart from './components/BandChart.jsx'
import SpectrumChart from './components/SpectrumChart.jsx'
import MaskPreview from './components/MaskPreview.jsx'

const EMPTY_RULES = { guard_required_mhz: 1.0, leakage_limit_dbm: -45.0, reuse_policy: {} }

const newCarrier = (i) => ({
  name: `C${i + 1}`, center_mhz: 100 + i * 6, bandwidth_mhz: 4,
  power_dbm: 20, polarization: 'H', mask_name: 'strict',
})

export default function App() {
  const [masks, setMasks] = useState([])
  const [carriers, setCarriers] = useState([newCarrier(0)])
  const [rules, setRules] = useState(EMPTY_RULES)
  const [band, setBand] = useState({ low: 80, high: 220 })
  const [scenarios, setScenarios] = useState([])
  const [scenarioId, setScenarioId] = useState(null)
  const [scenarioName, setScenarioName] = useState('未命名场景')
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

  const runAnalyze = useCallback(async () => {
    setBusy('analyze'); setError(''); setPlan(null)
    try {
      const res = await api.analyze({
        carriers,
        rules: { ...rules, reuse_policy: normalizePolicy(rules.reuse_policy) },
        plot_grid_mhz: 0.05,
      })
      setAnalysis(res)
      setTab('spectrum')
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }, [carriers, rules])

  const runPlan = useCallback(async () => {
    setBusy('plan'); setError('')
    try {
      const res = await api.plan({
        carriers,
        rules: { ...rules, reuse_policy: normalizePolicy(rules.reuse_policy) },
        band_low_mhz: band.low, band_high_mhz: band.high, mode: planMode,
      })
      setPlan(res)
      setPlanView(false) // 默认显示原始（冲突）谱；可切换到规划后
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }, [carriers, rules, band, planMode])

  const loadScenario = async (id) => {
    if (!id) { setScenarioId(null); return }
    setBusy('load'); setError('')
    try {
      const sc = await api.getScenario(id)
      setScenarioId(sc.id); setScenarioName(sc.name)
      setCarriers(sc.carriers.map(({ id, ...c }) => c))
      setRules({ guard_required_mhz: sc.guard_required_mhz,
                 leakage_limit_dbm: sc.leakage_limit_dbm, reuse_policy: sc.reuse_policy || {} })
      setBand({ low: sc.band_low_mhz, high: sc.band_high_mhz })
      setAnalysis(null); setPlan(null); setSelectedPair(null)
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const saveScenario = async () => {
    setBusy('save'); setError('')
    const payload = {
      name: scenarioName, description: '', band_low_mhz: band.low, band_high_mhz: band.high,
      guard_required_mhz: rules.guard_required_mhz, leakage_limit_dbm: rules.leakage_limit_dbm,
      reuse_policy: normalizePolicy(rules.reuse_policy), carriers,
    }
    try {
      const saved = scenarioId
        ? await api.updateScenario(scenarioId, payload)
        : await api.createScenario(payload)
      setScenarioId(saved.id)
      await refreshScenarios()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const deleteScenario = async () => {
    if (!scenarioId) return
    setBusy('del'); setError('')
    try {
      await api.deleteScenario(scenarioId)
      setScenarioId(null)
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
          </div>

          <div className="panel">
            <h2>载波录入</h2>
            <CarrierTable carriers={carriers} masks={masks} onChange={setCarriers}
                          onAdd={() => setCarriers([...carriers, newCarrier(carriers.length)])}
                          onRemove={(i) => setCarriers(carriers.filter((_, j) => j !== i))}
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
            <div className="hint">检查：频带重叠 · 保护带不足 · 掩模尾部越界（定向到载波对）；功率在线性域汇总。</div>
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
                <SpectrumChart spectrum={shownSpectrum} bands={spectrumBands} />
                <div className="plot-note">
                  提示：点击上方频段条选择载波；点击下方冲突条目可高亮对应载波对。
                </div>
              </>
            )}
            {tab === 'masks' && <MaskPreview masks={masks} />}
          </div>

          <div className="panel">
            <h2>OR-Tools 频率规划</h2>
            <div className="row">
              <label className="field-label">可用频段</label>
              <input className="field" type="number" style={{ width: 84 }} value={band.low}
                     onChange={(e) => setBand({ ...band, low: parseFloat(e.target.value) })} />
              <span className="muted">–</span>
              <input className="field" type="number" style={{ width: 84 }} value={band.high}
                     onChange={(e) => setBand({ ...band, high: parseFloat(e.target.value) })} />
              <span className="muted">MHz</span>
              <span className="seg">
                <button className={planMode === 'guard_only' ? 'on' : ''}
                        onClick={() => setPlanMode('guard_only')}>仅保护间隔</button>
                <button className={planMode === 'mask_aware' ? 'on' : ''}
                        onClick={() => setPlanMode('mask_aware')}>掩模感知</button>
              </span>
              <button className="primary" onClick={runPlan} disabled={!!busy || !carriers.length}>
                {busy === 'plan' ? '求解中…' : '求解频率位置'}
              </button>
            </div>
            <div className="hint">
              目标：在 1 kHz 网格上最小化各载波相对录入位置的总偏移；掩模感知模式按双向尾部泄漏达标反算间隔（含 0.5 dB 裕量）。
            </div>
            {plan && <PlanResult plan={plan} />}
          </div>

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
      </div>
    )
  }
  const counts = plan.post_check?.counts
  return (
    <div style={{ marginTop: 10 }}>
      <div className="row">
        <span style={{ color: 'var(--ok)' }}>✓ {plan.message}</span>
        <span className="spacer" />
        {counts && (
          <span className="muted">
            规划后复核：冲突 {counts.error} · 警告 {counts.warning} · 待评估 {counts.pending}
          </span>
        )}
        <button onClick={() => setOpen(!open)}>{open ? '收起' : '展开'}</button>
      </div>
      {open && (
        <table className="plan-table" style={{ marginTop: 8 }}>
          <thead>
            <tr><th>载波</th><th>原中心</th><th>新中心 MHz</th><th>频带范围</th><th>偏移 MHz</th></tr>
          </thead>
          <tbody>
            {plan.assignments.map((a) => (
              <tr key={a.name}>
                <td>{a.name} <span className="muted">{a.polarization}</span></td>
                <td>{a.original_center_mhz.toFixed(3)}</td>
                <td>{a.center_mhz.toFixed(3)}</td>
                <td>{a.low_mhz.toFixed(2)}–{a.high_mhz.toFixed(2)}</td>
                <td className={a.shift_mhz > 0 ? 'shift-pos' : a.shift_mhz < 0 ? 'shift-neg' : ''}>
                  {a.shift_mhz > 0 ? '+' : ''}{a.shift_mhz.toFixed(3)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
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
