/**
 * The usage chart.
 *
 * The current window and the previous one share an axis on purpose: a comparison is only
 * honest if both are drawn to the same scale. The previous window is a dashed outline so
 * it reads as a reference, not as data that was measured.
 */

import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

function tooltipStyle() {
  return {
    background: '#1e2732',
    border: '1px solid #2a3542',
    borderRadius: 6,
    fontSize: 12,
    color: '#e8edf2',
  }
}

export function UsageChart({ buckets, bucket, previous = [], previousLabel = 'Previous period' }) {
  if (!buckets || buckets.length === 0) {
    return <div className="empty">No readings in this window yet.</div>
  }

  const byLabel = new Map(previous.map((point) => [point.label, point.energy_kwh]))
  const data = buckets.map((bucketRow) => ({
    label: bucketRow.label,
    energy: bucketRow.energy_kwh,
    previous: byLabel.has(bucketRow.label) ? byLabel.get(bucketRow.label) : null,
    coverage: bucketRow.coverage_ratio,
  }))

  const axisLabel = bucket === 'Hour' ? 'Hour of day' : 'Day'

  return (
    <>
      <ResponsiveContainer width="100%" height={280}>
        <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
          <CartesianGrid stroke="#2a3542" strokeDasharray="2 4" />
          <XAxis
            dataKey="label"
            stroke="#93a1b0"
            fontSize={11}
            interval="preserveStartEnd"
            tickLine={false}
          />
          <YAxis stroke="#93a1b0" fontSize={11} tickLine={false} unit=" kWh" width={62} />
          <Tooltip
            contentStyle={tooltipStyle()}
            formatter={(value, name) =>
              [value === null || value === undefined ? '—' : `${value} kWh`, name].join(' ')
            }
          />
          <Bar dataKey="energy" name={axisLabel} fill="#4cc2ff" radius={[3, 3, 0, 0]} />
          {previous.length > 0 ? (
            <Line
              type="monotone"
              dataKey="previous"
              name={previousLabel}
              stroke="#93a1b0"
              strokeWidth={1.5}
              strokeDasharray="4 3"
              dot={false}
              connectNulls
            />
          ) : null}
        </ComposedChart>
      </ResponsiveContainer>
      <div className="legend">
        <span>
          <span className="swatch" style={{ background: '#4cc2ff' }} />
          This period
        </span>
        {previous.length > 0 ? (
          <span>
            <span
              className="swatch"
              style={{ background: 'transparent', borderTop: '2px dashed #93a1b0' }}
            />
            {previousLabel}
          </span>
        ) : null}
      </div>
    </>
  )
}

export function ProfileChart({ profile }) {
  if (!profile || profile.length === 0) {
    return <div className="empty">No readings in this window yet.</div>
  }
  return (
    <ResponsiveContainer width="100%" height={180}>
      <ComposedChart data={profile} margin={{ top: 8, right: 8, bottom: 4, left: -12 }}>
        <CartesianGrid stroke="#2a3542" strokeDasharray="2 4" />
        <XAxis dataKey="label" stroke="#93a1b0" fontSize={11} tickLine={false} interval={2} />
        <YAxis stroke="#93a1b0" fontSize={11} tickLine={false} unit=" W" width={58} />
        <Tooltip
          contentStyle={tooltipStyle()}
          formatter={(value, name) => [value === null ? '—' : `${value} W`, name].join(' ')}
        />
        <Line type="monotone" dataKey="avg_power_w" name="Average load" stroke="#4ade80" dot={false} />
      </ComposedChart>
    </ResponsiveContainer>
  )
}
