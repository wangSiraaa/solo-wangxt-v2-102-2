import { useMemo } from 'react'
import Plot from './Plot.jsx'

const POLS = ['H', 'V', 'LHCP', 'RHCP']
const Y_OF = { H: 4, V: 3, LHCP: 2, RHCP: 1 }
const BAR_H = 0.62

const SEV_COLOR = {
  error: 'rgba(255,93,93,0.85)',
  warning: 'rgba(245,166,35,0.85)',
  pending: 'rgba(176,140,255,0.85)',
  ok: 'rgba(77,163,255,0.8)',
}

/** 每个载波按其涉及的最严重冲突着色。 */
function severityByCarrier(findings) {
  const rank = { error: 3, warning: 2, pending: 1 }
  const m = {}
  for (const f of findings || []) {
    const r = rank[f.severity] || 0
    for (const n of [f.carrier_a, f.carrier_b]) {
      if (!m[n] || r > rank[m[n]]) m[n] = f.severity
    }
  }
  return m
}

export default function BandChart({ bands, findings, selectedPair, plan, onPick }) {
  const sev = useMemo(() => severityByCarrier(findings), [findings])
  const selected = selectedPair || []

  const shapes = []
  const annotations = []

  // 规划方案：绿色描边框（不填充，避免盖住原始条带）+ 虚线中心
  for (const a of plan?.assignments || []) {
    const y = Y_OF[a.polarization]
    shapes.push({
      type: 'rect', x0: a.low_mhz, x1: a.high_mhz, y0: y - BAR_H / 2, y1: y + BAR_H / 2,
      fillcolor: 'rgba(62,207,142,0.08)',
      line: { color: 'rgba(62,207,142,0.95)', width: 2, dash: 'solid' },
    })
    shapes.push({
      type: 'line', x0: a.center_mhz, x1: a.center_mhz,
      y0: y - BAR_H / 2, y1: y + BAR_H / 2,
      line: { color: '#3ecf8e', width: 1.5, dash: 'dot' },
    })
  }

  for (const b of bands || []) {
    const y = Y_OF[b.polarization] ?? 0.5
    const s = sev[b.name] || 'ok'
    const isSel = selected.includes(b.name)
    shapes.push({
      type: 'rect', x0: b.low_mhz, x1: b.high_mhz,
      y0: y - BAR_H / 2, y1: y + BAR_H / 2,
      fillcolor: SEV_COLOR[s] || SEV_COLOR.ok,
      line: { color: isSel ? '#ffffff' : 'rgba(0,0,0,0.45)', width: isSel ? 2.5 : 1 },
    })
    // 中心刻度
    shapes.push({
      type: 'line', x0: b.center_mhz, x1: b.center_mhz,
      y0: y - BAR_H / 2, y1: y + BAR_H / 2,
      line: { color: 'rgba(0,0,0,0.55)', width: 1 },
    })
    annotations.push({
      x: (b.low_mhz + b.high_mhz) / 2, y: y,
      text: `<b>${b.name}</b><br>${b.power_dbm} dBm`,
      showarrow: false, font: { size: 10, color: '#0b0f14' },
      yanchor: 'middle',
    })
  }

  const layout = {
    height: 250,
    margin: { l: 52, r: 16, t: 28, b: 36 },
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: '#b8c4cf', size: 11 },
    title: { text: '频段占用（彩色=录入频带及冲突；绿色描边=OR-Tools 规划位置）', font: { size: 12 } },
    xaxis: { title: '频率 (MHz)', zeroline: false, gridcolor: 'rgba(255,255,255,0.06)' },
    yaxis: {
      tickvals: [1, 2, 3, 4], ticktext: ['RHCP', 'LHCP', 'V', 'H'],
      range: [0.2, 4.8], fixedrange: true,
      gridcolor: 'rgba(255,255,255,0.06)',
    },
    shapes,
    annotations,
  }

  // 透明散点层仅用于点击选中载波（点取条带所在的频带）
  const pickLayer = {
    x: (bands || []).map((b) => b.center_mhz),
    y: (bands || []).map((b) => Y_OF[b.polarization] ?? 0.5),
    text: (bands || []).map((b) =>
      `${b.name} | ${b.low_mhz}–${b.high_mhz} MHz<br>中心 ${b.center_mhz} MHz, 带宽 ${b.bandwidth_mhz} MHz<br>` +
      `${b.power_dbm} dBm, ${b.polarization}, 掩模 ${b.mask_name}`),
    mode: 'markers',
    marker: { size: 26, color: 'rgba(0,0,0,0)' },
    hovertemplate: '%{text}<extra></extra>',
    showlegend: false,
  }

  return (
    <Plot
      data={[pickLayer]}
      layout={layout}
      revision={JSON.stringify({ shapes: shapes.length, bands: (bands || []).length,
                                 plan: (plan?.assignments || []).length, sel: selected.join(',') })}
      onClick={(e) => {
        const i = e?.points?.[0]?.pointIndex
        if (onPick && i != null && bands[i]) onPick(bands[i].name)
      }}
    />
  )
}
