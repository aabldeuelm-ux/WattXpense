/**
 * The written explanation, shown next to the figures it was written from.
 *
 * These are two separate blocks on purpose. The prose is a model's interpretation and the
 * table below it is what the app actually computed; keeping them apart is what makes the
 * prose checkable.
 */

import { Card, Empty, money, num } from './ui'

function CalculatedBlock({ context }) {
  if (!context || !Object.keys(context).length) return null
  const usage = context.usage || {}
  const forecast = context.forecast || {}
  const rows = [
    ['Total energy analysed', usage.total_energy_kwh !== undefined ? `${num(usage.total_energy_kwh, 2)} kWh` : null],
    ['Days analysed', usage.days_analysed],
    ['Average per day', usage.average_kwh_per_day !== undefined ? `${num(usage.average_kwh_per_day, 2)} kWh` : null],
    ['Night share', usage.night_share_percent !== undefined ? `${num(usage.night_share_percent)}%` : null],
    [
      'Projected energy',
      forecast.predicted_energy_kwh !== undefined ? `${num(forecast.predicted_energy_kwh, 2)} kWh` : null,
    ],
    ['Projected bill', forecast.projected_bill !== undefined && forecast.projected_bill !== null ? money(forecast.projected_bill) : null],
    ['Currency', context.currency],
    ['Data', context.data_label],
  ].filter(([, value]) => value !== null && value !== undefined)

  return (
    <div className="calculated-block">
      <dl>
        {rows.map(([label, value]) => (
          <div key={label} style={{ display: 'contents' }}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

const STATUS_HELP = {
  ok: 'Written by the local model.',
  not_configured: 'No model is configured, so this summary is built from the calculations alone.',
  disabled: 'The written explanation is switched off.',
  unreachable: 'A model is configured but not reachable, so this summary is built from the calculations alone.',
  model_missing: 'The configured model is not installed, so this summary is built from the calculations alone.',
  timeout: 'The model did not answer in time, so this summary is built from the calculations alone.',
  malformed: 'The model sent something unusable, so this summary is built from the calculations alone.',
  ungrounded: 'The model quoted a figure that is not in the data, so its answer was discarded.',
  no_data: 'No readings have been recorded yet.',
  unknown_provider: 'The configured provider is not one this app can use.',
}

export function ExplainPanel({ result, status, pending, onRefresh, modelStatus }) {
  if (pending) {
    return (
      <div className="panel">
        <h2>Written explanation</h2>
        <p className="panel-note">Waiting for the local model. A 7B model on CPU often takes 10–30 seconds.</p>
        <div className="empty">Asking the model…</div>
      </div>
    )
  }

  if (!result) return null

  const generated = result.generated || {}
  const fromModel = result.source === 'model'
  const badge = fromModel ? (
    <span className="tag good">written by {result.model || 'the local model'}</span>
  ) : (
    <span className="tag">calculated here, no model used</span>
  )

  return (
    <div className="panel">
      <h2>
        Written explanation
        {badge}
      </h2>
      <p className="panel-note">
        {result.message || STATUS_HELP[result.status] || ''}{' '}
        {modelStatus && !modelStatus.ai_available && modelStatus.status !== 'ok'
          ? `The model is currently ${modelStatus.status.replace('_', ' ')}.`
          : ''}
      </p>

      {result.status === 'no_data' ? (
        <Empty>No readings have been recorded, so there is nothing to explain yet.</Empty>
      ) : (
        <>
          {generated.headline ? <div className="generated"><div className="headline">{generated.headline}</div></div> : null}
          {generated.explanation ? <p>{generated.explanation}</p> : null}
          {generated.suggestions && generated.suggestions.length ? (
            <ul>
              {generated.suggestions.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
          {generated.caveats && generated.caveats.length ? (
            <>
              <div className="muted small">Caveats</div>
              <ul>
                {generated.caveats.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </>
          ) : null}
        </>
      )}

      {(result.warnings || []).map((warning) => (
        <div className="warning" key={warning}>
          {warning}
        </div>
      ))}

      <h2 style={{ marginTop: 18 }}>The figures this was written from</h2>
      <p className="panel-note">
        Calculated by WattXpense. If a number in the text above disagrees with this block, trust
        this block.
      </p>
      <CalculatedBlock context={result.calculated} />

      {onRefresh ? (
        <div style={{ marginTop: 14 }}>
          <button className="action" onClick={onRefresh}>
            {fromModel ? 'Ask the model again' : 'Refresh explanation'}
          </button>
        </div>
      ) : null}
    </div>
  )
}

export function AiStatusCard({ status }) {
  if (!status) return null
  return (
    <Card
      label="Written explanations"
      value={status.ai_available ? 'available' : status.status.replace(/_/g, ' ')}
      hint={
        status.ai_available
          ? `Provider ${status.provider}${status.model ? `, model ${status.model}` : ''}.`
          : status.message || 'Set WATTXPENSE_LLM_MODEL to enable the written explanation.'
      }
      tag={status.ai_available ? <span className="tag good">on</span> : <span className="tag">off</span>}
    />
  )
}
