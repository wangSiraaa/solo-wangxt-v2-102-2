import { useMemo } from 'react'
import Plot from './Plot.jsx'

const Y_OF = { H: 4, V: 3, LHCP: 2, RHCP: 1 }
const BAR_H = 0.62

const SEV_COLOR = {
  error: 'rgba(255,93,93,0.85)',
  warning: 'rgba(245,166,35,0.85)',
  pending: 'rgba(176,140,255,0.85)',
  ok: 'rgba(77,163,255,0.8)',
}

// 载波相对分配方案的归属状态配色（录入频带）
const ALLOC_STATUS_COLOR = {
  inside: null, // 沿用冲突着色
  spans_gap: 'rgba(255,93,93,0.55)',        // 跨段间空洞
  crosses_exclusion: 'rgba(255,93,93,0.55)', // 跨排除窗
  outside_segments: 'rgba(130,130,130,0.55)', // 完全在所有段之外
}
const ALLOC_STATUS_LABEL = {
  spans_gap: '跨段间空洞',
  crosses_exclusion: '跨越排除窗',
  outside_segments: '在可用段之外',
}

// 每个可用段一种归属色（规划后绿色描边框按所属段着色）
const SEG_COLORS = ['#3ecf8e', '#4da3ff', '#f5a623', '#b08cff',
                    '#ff8f5d', '#5dd6d6', '#e35dd6', '#9be05d']

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

export default function BandChart({ bands, findings, selectedPair, plan, allocation, onPick }) {
  const sev = useMemo(() => severityByCarrier(findings), [findings])
  const selected = selectedPair || []

  const shapes = []
  const annotations = []

  // ---- 频谱分配：可用段淡色底带 + 排除窗红色斜纹区 + 段边界 ----
  const xMin = allocation ? Math.min(...allocation.segments.map((s) => s.low_mhz)) : null
  const xMax = allocation ? Math.max(...allocation.segments.map((s) => s.high_mhz)) : null
  for (const [i, s] of (allocation?.segments || []).entries()) {
    shapes.push({
      type: 'rect', x0: s.low_mhz, x1: s.high_mhz, y0: 0.15, y1: 4.85,
      fillcolor: SEG_COLORS[i % SEG_COLORS.length], opacity: 0.05,
      layer: 'below', line: { width: 0 },
    })
    // 段边界
    for (const x of [s.low_mhz, s.high_mhz]) {
      shapes.push({
        type: 'line', x0: x, x1: x, y0: 0.15, y1: 4.85,
        line: { color: SEG_COLORS[i % SEG_COLORS.length], width: 1.2, dash: 'dash' },
      })
    }
    annotations.push({
      x: (s.low_mhz + s.high_mhz) / 2, y: 4.78,
      text: `<b>S${i + 1}</b> ${s.low_mhz}–${s.high_mhz}`,
      showarrow: false, font: { size: 9, color: SEG_COLORS[i % SEG_COLORS.length] },
      yanchor: 'top',
    })
  }
  for (const e of allocation?.exclusions || []) {
    shapes.push({
      type: 'rect', x0: e.low_mhz, x1: e.high_mhz, y0: 0.15, y1: 4.85,
      fillcolor: 'rgba(255,93,93,0.18)', line: { color: 'rgba(255,93,93,0.8)', width: 1 },
      // 斜纹：用密集竖线在下面补
    })
    const step = Math.max((e.high_mhz - e.low_mhz) / 8, (xMax - xMin || 1) / 400)
    for (let x = e.low_mhz; x <= e.high_mhz; x += step) {
      shapes.push({
        type: 'line', x0: x, x1: Math.min(x + step, e.high_mhz), y0: 0.15, y1: 4.85,
        line: { color: 'rgba(255,93,93,0.22)', width: 0.6 },
      })
    }
    annotations.push({
      x: (e.low_mhz + e.high_mhz) / 2, y: 0.32,
      text: `排除窗 ${e.low_mhz}–${e.high_mhz}${e.reason ? `（${e.reason}）` : ''}`,
      showarrow: false, font: { size: 9, color: '#ff8080' }, yanchor: 'bottom',
    })
  }

  // ---- 规划方案：按所属可用段着色的描边框 + 虚线中心 ----
  for (const a of plan?.assignments || []) {
    const y = Y_OF[a.polarization]
    const segColor = SEG_COLORS[(a.segment_index ?? 0) % SEG_COLORS.length]
    shapes.push({
      type: 'rect', x0: a.low_mhz, x1: a.high_mhz, y0: y - BAR_H / 2, y1: y + BAR_H / 2,
      fillcolor: segColor, opacity: 0.1,
      line: { color: segColor, width: 2, dash: 'solid' },
    })
    shapes.push({
      type: 'line', x0: a.center_mhz, x1: a.center_mhz,
      y0: y - BAR_H / 2, y1: y + BAR_H / 2,
      line: { color: segColor, width: 1.5, dash: 'dot' },
    })
  }

  // ---- 录入频带（按冲突 / 归属状态着色）----
  for (const b of bands || []) {
    const y = Y_OF[b.polarization] ?? 0.5
    const allocColor = b.allocation_status ? ALLOC_STATUS_COLOR[b.allocation_status] : null
    const s = sev[b.name] || 'ok'
    const isSel = selected.includes(b.name)
    shapes.push({
      type: 'rect', x0: b.low_mhz, x1: b.high_mhz,
      y0: y - BAR_H / 2, y1: y + BAR_H / 2,
      fillcolor: allocColor || SEV_COLOR[s] || SEV_COLOR.ok,
      line: {
        color: isSel ? '#ffffff' : 'rgba(0,0,0,0.45)',
        width: isSel ? 2.5 : 1,
        dash: b.allocation_status && b.allocation_status !== 'inside' ? 'dash' : 'solid',
      },
    })
    shapes.push({
      type: 'line', x0: b.center_mhz, x1: b.center_mhz,
      y0: y - BAR_H / 2, y1: y + BAR_H / 2,
      line: { color: 'rgba(0,0,0,0.55)', width: 1 },
    })
    const statusTag = b.allocation_status && b.allocation_status !== 'inside'
      ? `<br><span style="color:#ff8080">⚠ ${ALLOC_STATUS_LABEL[b.allocation_status]}</span>`
      : ''
    annotations.push({
      x: (b.low_mhz + b.high_mhz) / 2, y,
      text: `<b>${b.name}</b><br>${b.power_dbm} dBm${statusTag}`,
      showarrow: false, font: { size: 10, color: '#0b0f14' },
      yanchor: 'middle',
    })
  }

  const title = allocation
    ? '频段占用（彩色=录入频带及冲突；底带=可用段 S1/S2…；红区=排除窗；彩色描边=规划后归属）'
    : '频段占用（彩色=录入频带及冲突；绿色描边=OR-Tools 规划位置）'

  const layout = {
    height: 260,
    margin: { l: 52, r: 16, t: 28, b: 36 },
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: '#b8c4cf', size: 11 },
    title: { text: title, font: { size: 12 } },
    xaxis: {
      title: '频率 (MHz)', zeroline: false,
      gridcolor: 'rgba(255,255,255,0.06)',
      range: xMin != null ? [xMin - (xMax - xMin) * 0.04, xMax + (xMax - xMin) * 0.04] : undefined,
    },
    yaxis: {
      tickvals: [1, 2, 3, 4], ticktext: ['RHCP', 'LHCP', 'V', 'H'],
      range: [0.1, 4.95], fixedrange: true,
      gridcolor: 'rgba(255,255,255,0.06)',
    },
    shapes,
    annotations,
  }

  // 透明散点层仅用于点击选中载波
  const pickLayer = {
    x: (bands || []).map((b) => b.center_mhz),
    y: (bands || []).map((b) => Y_OF[b.polarization] ?? 0.5),
    text: (bands || []).map((b) => {
      let t = `${b.name} | ${b.low_mhz}–${b.high_mhz} MHz<br>中心 ${b.center_mhz} MHz, 带宽 ${b.bandwidth_mhz} MHz<br>` +
        `${b.power_dbm} dBm, ${b.polarization}, 掩模 ${b.mask_name}`
      if (b.allocation_status && b.allocation_status !== 'inside') {
        t += `<br>⚠ ${ALLOC_STATUS_LABEL[b.allocation_status] || b.allocation_status}`
      } else if (b.piece_mhz) {
        t += `<br>归属：S${(b.segment_index ?? 0) + 1} 内小片 ${b.piece_mhz[0]}–${b.piece_mhz[1]} MHz`
      }
      return t
    }),
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
                                 plan: (plan?.assignments || []).length,
                                 alloc: allocation?.segments?.length,
                                 exc: allocation?.exclusions?.length,
                                 ver: allocation?.version,
                                 sel: selected.join(',') })}
      onClick={(e) => {
        const i = e?.points?.[0]?.pointIndex
        if (onPick && i != null && bands[i]) onPick(bands[i].name)
      }}
    />
  )
}
