/**
 * Forecast, anomalies, and savings insights.
 *
 * Every panel here is labelled as an estimate. A forecast and a measurement look identical
 * on a chart unless something says otherwise, so the label sits on the panel, not in a
 * footnote.
 */

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { Card, Empty, EstimateTag, Panel, num, pct } from './ui'

function tooltipStyle() {
  return {
    background: '#1e2732',
    border: '1px solid #2a3542',
    borderRadius: 6,
    fontSize: 12,
    color: '#e8edf2',
  }
}

export function ForecastPanel({ forecast }) {
  if (!forecast) return null
  if (forecast.status !== 'ok') {
    return (
      <Panel title="Forecast" tag={<EstimateTag />}>
        <Empty>{forecast.message || `Not enough history yet (${forecast.status.replace('_', ' ')}).`}</Empty>
      </Panel>
    )
  }

  const points = forecast.points || []
  const metrics = forecast.metrics || {}
  const data = points.map((point) => ({
    label: point.date.slice(5),
    energy: point.energy_kwh,
    lower: point.lower_kwh,
    upper: point.upper_kwh,
    kind: point.kind,
  }))

  return (
    <Panel
      title="Forecast"
      tag={<EstimateTag />}
      note={
        <>
          {forecast.method} over {forecast.history_days} days of history, confidence{' '}
          {forecast.confidence}. {forecast.band_note}
        </>
      }
    >
      <ResponsiveContainer width="100%" height={240}>
        <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
          <defs>
            <linearGradient id="band" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#4cc2ff" stopOpacity={0.45} />
              <stop offset="100%" stopColor="#4cc2ff" stopOpacity={0.08} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#2a3542" strokeDasharray="2 4" />
          <XAxis dataKey="label" stroke="#93a1b0" fontSize={11} tickLine={false} />
          <YAxis stroke="#93a1b0" fontSize={11} tickLine={false} unit=" kWh" width={62} />
          <Tooltip
            contentStyle={tooltipStyle()}
            formatter={(value, name) => [value === null ? '—' : `${value} kWh`, name].join(' ')}
          />
          <Area dataKey="upper" name="Upper band" stroke="none" fill="#4cc2ff" fillOpacity={0.12} />
          <Area dataKey="lower" name="Lower band" stroke="none" fill="#0f1419" fillOpacity={0.9} />
          <Area dataKey="energy" name="Forecast" stroke="#4cc2ff" strokeWidth={2} fill="url(#band)" />
        </AreaChart>
      </ResponsiveContainer>

      <div className="legend">
        <span>
          <span className="swatch" style={{ background: '#4cc2ff' }} />
          Forecast
        </span>
        <span>
          <span className="swatch" style={{ background: 'rgba(76,194,255,0.2)' }} />
          Uncertainty band
        </span>
      </div>

      <div className="cards" style={{ marginTop: 16, marginBottom: 0 }}>
        <Card
          label="Predicted total"
          value={num(forecast.predicted_energy_kwh, 1)}
          unit="kWh"
          tag={<EstimateTag />}
          hint={`${num(forecast.predicted_avg_kwh_per_day, 2)} kWh per day`}
        />
        <Card
          label="Model accuracy"
          value={metrics.mae_kwh !== undefined ? num(metrics.mae_kwh, 2) : '—'}
          unit="kWh MAE"
          hint={
            metrics.mape_percent !== undefined
              ? `${num(metrics.mape_percent)}% MAPE on ${metrics.test_samples} held-out days`
              : undefined
          }
        />
        <Card
          label="Peak / off-peak"
          value={`${num(forecast.predicted_peak_kwh, 1)} / ${num(forecast.predicted_offpeak_kwh, 1)}`}
          unit="kWh"
          hint="Split at the tariff's peak window"
        />
      </div>

      {forecast.confidence_reason ? (
        <p className="muted small" style={{ marginTop: 10 }}>
          Why this confidence: {forecast.confidence_reason}
        </p>
      ) : null}
      {(forecast.warnings || []).map((warning) => (
        <p className="warning" key={warning}>
          {warning}
        </p>
      ))}
    </Panel>
  )
}

export function AnomaliesPanel({ anomalies }) {
  if (!anomalies) return null
  const findings = anomalies.findings || []
  return (
    <Panel
      title="Unusual days and data gaps"
      tag={<EstimateTag />}
      note={
        <>
          {anomalies.status === 'ok'
            ? `${anomalies.days_analysed} days analysed by ${anomalies.method}.`
            : anomalies.message || anomalies.status}
          {' '}Every finding shows the figures it is based on.
        </>
      }
    >
      {findings.length === 0 ? (
        <Empty>Nothing unusual found in the analysed window.</Empty>
      ) : (
        findings.map((finding) => (
          <div className="finding" key={finding.id || finding.title}>
            <div className="finding-title">
              {finding.title}{' '}
              {finding.severity ? <span className="tag">{finding.severity}</span> : null}
            </div>
            {finding.summary ? <div className="small">{finding.summary}</div> : null}
            <div className="evidence">
              {(finding.evidence || []).map((item) => (
                <div key={item}>{item}</div>
              ))}
            </div>
            {finding.expected !== undefined ? (
              <div className="muted small" style={{ marginTop: 4 }}>
                Expected {num(finding.expected, 2)}, recorded {num(finding.actual, 2)} kWh
              </div>
            ) : null}
          </div>
        ))
      )}
      {(anomalies.notes || []).map((note) => (
        <p className="muted small" key={note}>
          {note}
        </p>
      ))}
    </Panel>
  )
}

export function InsightsPanel({ insights, currency }) {
  if (!insights) return null
  const list = insights.insights || []
  const bands = insights.load_bands || []
  const range = insights.savings_range_cost_per_month

  return (
    <Panel
      title="Where the money goes"
      tag={<EstimateTag />}
      note={
        <>
          {insights.status === 'ok'
            ? `${insights.days_analysed} days analysed.`
            : insights.status}
          {' '}Savings figures are estimates from the patterns below, not measurements.
        </>
      }
    >
      {list.length === 0 ? (
        <Empty>Not enough history to suggest anything yet.</Empty>
      ) : (
        list.map((insight) => (
          <div className="suggestion" key={insight.id || insight.title}>
            <div className="title">
              {insight.title}{' '}
              <span className="tag">{insight.priority}</span>
            </div>
            <div className="detail">{insight.summary}</div>
            {insight.savings_cost_per_month ? (
              <div className="detail">
                Estimated saving {Number(insight.savings_cost_per_month).toFixed(2)} {currency}/month
                {insight.savings_basis ? ` (${insight.savings_basis})` : ''}
              </div>
            ) : null}
            {(insight.evidence || []).length ? (
              <div className="evidence detail">
                {(insight.evidence || []).map((item) => (
                  <div key={item}>{item}</div>
                ))}
              </div>
            ) : null}
          </div>
        ))
      )}

      {range && range.length === 2 ? (
        <p className="muted small" style={{ marginTop: 12 }}>
          Across all of these, a combined{' '}
          {Number(range[0]).toFixed(0)}–{Number(range[1]).toFixed(0)} {currency} a month is the
          plausible range, not a promise.
        </p>
      ) : null}

      {bands.length ? (
        <>
          <h2 style={{ marginTop: 18 }}>Load bands</h2>
          <p className="panel-note">
            Share of readings and of energy in each band. A narrow band of steady overnight load is
            usually standby, not something you can switch off.
          </p>
          <table>
            <thead>
              <tr>
                <th>Band</th>
                <th className="num">Readings</th>
                <th className="num">Energy</th>
              </tr>
            </thead>
            <tbody>
              {bands.map((band) => (
                <tr key={band.label}>
                  <td>
                    {band.label} ({num(band.lower_w, 0)}–{num(band.upper_w, 0)} W)
                  </td>
                  <td className="num">{num(band.sample_share_percent, 1)}%</td>
                  <td className="num">{num(band.energy_share_percent, 1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
    </Panel>
  )
}

export function CoverageCard({ coverage, readingCount, daysWithData }) {
  return (
    <Card
      label="Data coverage"
      value={pct(coverage)}
      hint={
        readingCount
          ? `${readingCount} readings across ${daysWithData} day${daysWithData === 1 ? '' : 's'}.`
          : 'No readings in this window.'
      }
      tag={coverage < 0.8 ? <span className="tag bad">partial</span> : null}
    />
  )
}
