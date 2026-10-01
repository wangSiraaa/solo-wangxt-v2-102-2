import { useMemo } from 'react'
import Plot from './Plot.jsx'

const COLORS = { strict: '#ff5d5d', loose: '#f5a623', clean: '#3ecf8e' }

/** 示例频谱发射掩模（关于 0 偏移对称的折线）。 */
export default function MaskPreview({ masks }) {
  const data = useMemo(() => masks.map((m) => {
    const half = m.points
    const x = half.map(([x]) => -x).reverse().concat(half.slice(1).map(([x]) => x))
    const y = half.map(([, y]) => y).reverse().concat(half.slice(1).map(([, y]) => y))
    return {
      x, y, mode: 'lines+markers', type: 'scatter', name: m.name,
      line: { color: COLORS[m.name] || '#8595a3', width: 2 },
      marker: { size: 4 },
      hovertemplate: `${m.name} 偏移 %{x:.1f} MHz<br>衰减 %{y:.0f} dB<extra></extra>`,
    }
  }), [masks])

  const layout = {
    height: 230,
    margin: { l: 58, r: 14, t: 30, b: 44 },
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: '#b8c4cf', size: 11 },
    title: { text: '示例频谱发射掩模（教学数值，不对应标准/设备指标）', font: { size: 12 } },
    xaxis: { title: '相对中心频率偏移 (MHz)', zeroline: false, gridcolor: 'rgba(255,255,255,0.06)' },
    yaxis: { title: '相对衰减 (dB)', zeroline: false, gridcolor: 'rgba(255,255,255,0.06)' },
    legend: { orientation: 'h', y: -0.24 },
  }
  return <Plot data={data} layout={layout} revision={masks.map((m) => m.name).join(',')} />
}
