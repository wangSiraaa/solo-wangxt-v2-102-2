import { useEffect, useRef } from 'react'
import Plotly from 'plotly.js-dist-min'

const BASE_CONFIG = { responsive: true, displaylogo: false }

/** Plotly 图表封装：首次 newPlot，后续 react（保留缩放等交互）。 */
export default function Plot({ data, layout, config, onClick, revision }) {
  const ref = useRef(null)
  const handlerRef = useRef(null)

  useEffect(() => {
    const el = ref.current
    Plotly.newPlot(el, data, layout, { ...BASE_CONFIG, ...config })
    if (onClick) {
      handlerRef.current = onClick
      el.on('plotly_click', (e) => handlerRef.current(e))
    }
    const onResize = () => { if (ref.current) Plotly.Plots.resize(ref.current) }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      Plotly.purge(el)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    Plotly.react(ref.current, data, layout, { ...BASE_CONFIG, ...config })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revision])

  return <div ref={ref} style={{ width: '100%' }} />
}
