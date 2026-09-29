/**
 * Small shared presentational pieces.
 *
 * The tags matter more than they look: a card showing a projection has to say so next to
 * the number, not in a footnote, and anything drawn from the simulator has to carry that
 * label everywhere it appears.
 */

export function Card({ label, value, unit, hint, tag }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className="value">
        {value}
        {unit ? <span className="unit">{unit}</span> : null}
        {tag}
      </div>
      {hint ? <div className="hint">{hint}</div> : null}
    </div>
  )
}

export function Panel({ title, note, tag, children }) {
  return (
    <section className="panel">
      <h2>
        {title}
        {tag}
      </h2>
      {note ? <p className="panel-note">{note}</p> : null}
      {children}
    </section>
  )
}

export function EstimateTag() {
  return <span className="tag estimate">estimate</span>
}

export function SimulatedTag() {
  return <span className="tag simulated">simulated</span>
}

export function Banner({ kind, children }) {
  return <div className={`banner ${kind}`}>{children}</div>
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

export function ErrorBox({ error, onRetry }) {
  if (!error) return null
  return (
    <div className="error">
      <div>{String(error.message || error)}</div>
      {onRetry ? (
        <button className="action" style={{ marginTop: 10 }} onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  )
}

const nf = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 })
const nf1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 })
const nf2 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export function num(value, places = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  if (places === 0) return nf.format(value)
  if (places === 1) return nf1.format(value)
  return nf2.format(value)
}

export function money(value, currency = 'INR') {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const symbol = currency === 'INR' ? '₹' : currency === 'USD' ? '$' : `${currency} `
  return `${symbol}${nf2.format(value)}`
}

export function pct(value, places = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(places)}%`
}
