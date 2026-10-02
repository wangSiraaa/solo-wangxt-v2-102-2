import { useMemo } from 'react'
import Plot from './Plot.jsx'

const POL_COLOR = { H: '#4da3ff', V: '#f5a623', LHCP: '#b08cff', RHCP: '#3ecf8e' }

/** 发射谱：各载波掩模曲线（细）+ 线性域功率叠加的聚合谱（粗白）。 */
export default function SpectrumChart({ spectrum, bands, allocation, showCarriers = true }) {
  const { f_mhz: f, curves = [], aggregate_dbm_hz: agg = [] } = spectrum || {}

  const data = useMemo(() => {
    const traces = []
    if (showCarriers) {
      for (const c of curves) {
        traces.push({
          x: f, y: c.psd_dbm_hz,
          mode: 'lines', type: 'scattergl',
          name: `${c.name} (${c.polarization}, ${c.mask_name})`,
          line: { color: POL_COLOR[c.polarization] || '#8595a3', width: 1 },
          opacity: 0.55,
          connectgaps: false,
          hovertemplate: `${c.name} %{x:.2f} MHz<br>%{y:.1f} dBm/Hz<extra></extra>`,
        })
      }
    }
    // 载波频带背景（形状）在 layout 里；聚合谱置顶
    traces.push({
      x: f, y: agg,
      mode: 'lines', type: 'scattergl',
      name: '聚合谱（线性域叠加）',
      line: { color: '#ffffff', width: 2.2 },
      connectgaps: false,
      hovertemplate: '聚合 %{x:.2f} MHz<br>%{y:.1f} dBm/Hz<extra></extra>',
    })
    return traces
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spectrum, showCarriers])

  const bandShapes = useMemo(() => (bands || []).map((b) => ({
    type: 'rect', x0: b.low_mhz, x1: b.high_mhz, y0: 0, y1: 1, yref: 'paper',
    fillcolor: POL_COLOR[b.polarization] || '#888',
    opacity: 0.06, line: { width: 0 },
  })), [bands])

  // 可用段边界（细虚线）+ 排除窗（红区，覆盖全图高度）
  const allocationShapes = useMemo(() => {
    const out = []
    for (const s of allocation?.segments || []) {
      for (const x of [s.low_mhz, s.high_mhz]) {
        out.push({
          type: 'line', x0: x, x1: x, y0: 0, y1: 1, yref: 'paper',
          line: { color: 'rgba(77,163,255,0.5)', width: 1, dash: 'dash' },
        })
      }
    }
    for (const e of allocation?.exclusions || []) {
      out.push({
        type: 'rect', x0: e.low_mhz, x1: e.high_mhz, y0: 0, y1: 1, yref: 'paper',
        fillcolor: 'rgba(255,93,93,0.13)',
        line: { color: 'rgba(255,93,93,0.7)', width: 1 },
      })
    }
    return out
  }, [allocation])

  const shapes = [...bandShapes, ...allocationShapes]

  const layout = {
    height: 340,
    margin: { l: 58, r: 16, t: 28, b: 44 },
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: '#b8c4cf', size: 11 },
    title: { text: '发射谱与掩模尾部（聚合在 W/Hz 线性域叠加后换算 dB）', font: { size: 12 } },
    xaxis: { title: '频率 (MHz)', zeroline: false, gridcolor: 'rgba(255,255,255,0.06)' },
    yaxis: { title: 'PSD (dBm/Hz)', zeroline: false, gridcolor: 'rgba(255,255,255,0.06)' },
    legend: { orientation: 'h', y: -0.22, font: { size: 10 } },
    shapes,
    hovermode: 'closest',
  }

  return <Plot data={data} layout={layout} revision={JSON.stringify({ n: curves.length, agg: agg.length, show: showCarriers })} />
}
