/**
 * Small shared presentational pieces.
 *
 * The tags matter more than they look: a card showing a projection has to say so next to
 * the number, not in a footnote, and anything drawn from the simulator has to carry that
 * label everywhere it appears.
 */

export function Card({ label, value, unit, hint, tag, tone }) {
  return (
    <div className={`card${tone ? ` tone-${tone}` : ''}`}>
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

export function Panel({ title, note, tag, actions, children, id }) {
  return (
    <section className="panel" id={id}>
      <div className="panel-head">
        <h2>
          {title}
          {tag}
        </h2>
        {actions ? <div className="panel-actions">{actions}</div> : null}
      </div>
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

export function Tag({ kind = '', children }) {
  return <span className={`tag ${kind}`}>{children}</span>
}

export function Banner({ kind, children }) {
  return <div className={`banner ${kind}`}>{children}</div>
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

export function Loading({ label = 'Loading…' }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  )
}

export function ErrorBox({ error, onRetry }) {
  if (!error) return null
  return (
    <div className="error" role="alert">
      <div>{String(error.message || error)}</div>
      {onRetry ? (
        <button className="action" style={{ marginTop: 10 }} onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  )
}

export function Field({ label, hint, children }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint ? <span className="field-hint">{hint}</span> : null}
    </label>
  )
}

export function CopyButton({ value, label = 'Copy' }) {
  return (
    <button
      type="button"
      className="action small-action"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value)
        } catch {
          /* Clipboard is unavailable over plain HTTP in some browsers. The value stays visible. */
        }
      }}
    >
      {label}
    </button>
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

export function relativeTime(iso, now = new Date()) {
  if (!iso) return 'never'
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return 'unknown'
  const minutes = Math.round((now.getTime() - then.getTime()) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  return then.toLocaleDateString()
}
