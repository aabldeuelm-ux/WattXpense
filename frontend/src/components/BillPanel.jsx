/**
 * Bill and tariff rendering.
 *
 * Slab tariffs are shown as one line per block. A single blended rate would be a tidier
 * table and a wrong bill, so the blocks stay visible.
 */

import { Card, Empty, EstimateTag, Panel, money, num } from './ui'

export function BillLines({ bill, currency }) {
  if (!bill) return <Empty>No bill has been calculated yet.</Empty>
  const lines = bill.lines || []
  return (
    <table>
      <thead>
        <tr>
          <th>Line</th>
          <th className="num">Quantity</th>
          <th className="num">Rate</th>
          <th className="num">Amount</th>
        </tr>
      </thead>
      <tbody>
        {lines.map((line, index) => (
          <tr key={`${line.label}-${index}`}>
            <td>
              {line.label}
              {line.note ? <div className="muted small">{line.note}</div> : null}
            </td>
            <td className="num">
              {num(line.quantity, 2)} {line.unit}
            </td>
            <td className="num">{line.rate === null || line.rate === undefined ? '—' : num(line.rate, 2)}</td>
            <td className="num">{money(line.amount, currency)}</td>
          </tr>
        ))}
        {bill.energy_charge !== undefined ? (
          <tr className="total-row">
            <td colSpan={3}>Energy charge</td>
            <td className="num">{money(bill.energy_charge, currency)}</td>
          </tr>
        ) : null}
        {bill.standing_charge ? (
          <tr>
            <td colSpan={3}>Standing charge</td>
            <td className="num">{money(bill.standing_charge, currency)}</td>
          </tr>
        ) : null}
        {bill.tax ? (
          <tr>
            <td colSpan={3}>
              {bill.duty_label} ({num(bill.tax_percent, 2)}%)
            </td>
            <td className="num">{money(bill.tax, currency)}</td>
          </tr>
        ) : null}
        <tr className="total-row">
          <td colSpan={3}>Total</td>
          <td className="num">{money(bill.total, currency)}</td>
        </tr>
      </tbody>
    </table>
  )
}

export function ProjectedBillPanel({ summary, currency }) {
  const bill = summary?.projected_bill
  const period = summary?.energy_this_period || {}
  const metered = summary?.today_bill_detail

  return (
    <Panel
      title={`Projected bill — ${bill?.tariff_name || 'no tariff set'}`}
      tag={bill?.is_estimate ? <EstimateTag /> : null}
      note={
        <>
          Metered days are added at full value and forecast days are added from the forecast. A
          day with a reading gap is excluded rather than scaled up, so this total does not
          quietly fill holes in your data.
          {period.days_skipped
            ? ` ${period.days_skipped} day${period.days_skipped === 1 ? '' : 's'} with gaps excluded.`
            : ''}
        </>
      }
    >
      {!bill ? (
        <Empty>No tariff is configured, so no bill can be projected.</Empty>
      ) : (
        <>
          <BillLines bill={bill} currency={currency} />
          {bill.tax_base !== undefined && bill.tax_percent ? (
            <p className="muted small" style={{ marginTop: 10 }}>
              {bill.duty_label} applies to the energy charge of {money(bill.tax_base, currency)}, not
              to the standing charge.
            </p>
          ) : null}
          {bill.cost_basis_note ? <p className="muted small">{bill.cost_basis_note}</p> : null}
        </>
      )}

      {metered ? (
        <>
          <h2 style={{ marginTop: 18 }}>Today so far</h2>
          <p className="panel-note">
            Priced at the rate the period has already reached, not at a flat average. The standing
            charge and duty are period-wide, so they are left out here and included in the
            projection above.
          </p>
          <table>
            <tbody>
              <tr>
                <td>Energy recorded today</td>
                <td className="num">
                  {num(metered.energy_kwh, 2)} kWh
                </td>
              </tr>
              <tr>
                <td>Cost of today's energy</td>
                <td className="num">{money(metered.energy_charge, currency)}</td>
              </tr>
            </tbody>
          </table>
          {(metered.notes || []).map((note) => (
            <p className="muted small" key={note} style={{ marginTop: 8 }}>
              {note}
            </p>
          ))}
        </>
      ) : null}
    </Panel>
  )
}

export function TariffPanel({ tariff, onEdit, onSave, saving }) {
  if (!tariff) return null
  const isSlab = tariff.rate_type === 'slab'
  return (
    <Panel
      title="Tariff"
      note={
        tariff.is_example
          ? 'This is a worked example, not a real DISCOM schedule. Replace it with the rates from your own bill before trusting any projection.'
          : undefined
      }
      tag={tariff.is_example ? <span className="tag">example</span> : <span className="tag good">yours</span>}
    >
      <table>
        <tbody>
          <tr>
            <td>Name</td>
            <td className="num">{tariff.name}</td>
          </tr>
          <tr>
            <td>Type</td>
            <td className="num">{tariff.rate_type.replace('_', '-')}</td>
          </tr>
          {isSlab ? (
            tariff.slabs.map((slab, index) => (
              <tr key={index}>
                <td>
                  {slab.up_to_kwh === null ? 'Above the last block (open ended)' : `First ${slab.up_to_kwh} kWh`}
                </td>
                <td className="num">
                  {num(slab.rate_per_kwh, 2)} {tariff.currency}/kWh
                </td>
              </tr>
            ))
          ) : tariff.rate_type === 'time_of_use' ? (
            <>
              <tr>
                <td>
                  Peak ({String(tariff.peak_start_hour).padStart(2, '0')}:00–
                  {String(tariff.peak_end_hour).padStart(2, '0')}:00)
                </td>
                <td className="num">{num(tariff.peak_rate_per_kwh, 2)} {tariff.currency}/kWh</td>
              </tr>
              <tr>
                <td>Off-peak</td>
                <td className="num">{num(tariff.offpeak_rate_per_kwh, 2)} {tariff.currency}/kWh</td>
              </tr>
            </>
          ) : (
            <tr>
              <td>Flat rate</td>
              <td className="num">
                {num(tariff.flat_rate_per_kwh, 2)} {tariff.currency}/kWh
              </td>
            </tr>
          )}
          <tr>
            <td>Standing charge</td>
            <td className="num">
              {num(tariff.standing_charge_per_day, 2)} {tariff.currency}/day
            </td>
          </tr>
          <tr>
            <td>{tariff.duty_label}</td>
            <td className="num">{num(tariff.tax_percent, 2)}%</td>
          </tr>
          <tr>
            <td>Billing period starts on the</td>
            <td className="num">{tariff.billing_start_day}</td>
          </tr>
        </tbody>
      </table>
      {onEdit ? (
        <div style={{ marginTop: 14 }}>
          <button className="action" onClick={onEdit} disabled={saving}>
            {saving ? 'Saving…' : 'Change tariff'}
          </button>
        </div>
      ) : null}
    </Panel>
  )
}

export function TariffEditor({ onSave, onCancel }) {
  return (
    <Panel
      title="Change tariff"
      note="Each unit is charged at the rate of the block it falls in. The top block must be open ended so nothing goes unpriced."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault()
          const data = new FormData(event.currentTarget)
          const slabs = []
          const blocks = Number(data.get('blocks') || 1)
          for (let index = 0; index < blocks; index += 1) {
            const bound = String(data.get(`slab_up_to_${index}`) ?? '').trim()
            slabs.push({
              up_to_kwh: index === blocks - 1 ? null : Number(bound),
              rate_per_kwh: Number(data.get(`slab_rate_${index}`)),
            })
          }
          onSave({
            name: String(data.get('name')),
            rate_type: 'slab',
            currency: String(data.get('currency') || 'INR'),
            standing_charge_per_day: Number(data.get('standing') || 0),
            tax_percent: Number(data.get('tax') || 0),
            billing_start_day: Number(data.get('start_day') || 1),
            slabs,
            is_active: true,
          })
        }}
      >
        <div className="two-col">
          <label>
            Name
            <input name="name" defaultValue="My slab tariff" style={{ width: '100%', marginTop: 4 }} />
          </label>
          <label>
            Currency
            <input name="currency" defaultValue="INR" style={{ width: '100%', marginTop: 4 }} />
          </label>
        </div>
        <table style={{ marginTop: 14 }}>
          <thead>
            <tr>
              <th>Block</th>
              <th className="num">Up to (kWh)</th>
              <th className="num">Rate / kWh</th>
            </tr>
          </thead>
          <tbody>
            {[0, 1, 2].map((index) => (
              <tr key={index}>
                <td>{index === 2 ? 'Above (open ended)' : `Block ${index + 1}`}</td>
                <td className="num">
                  {index === 2 ? (
                    <span className="muted">automatic</span>
                  ) : (
                    <input
                      name={`slab_up_to_${index}`}
                      defaultValue={index === 0 ? 100 : 300}
                      style={{ width: 90, textAlign: 'right' }}
                    />
                  )}
                </td>
                <td className="num">
                  <input
                    name={`slab_rate_${index}`}
                    defaultValue={index === 0 ? 2.5 : index === 1 ? 4.5 : 6.5}
                    style={{ width: 90, textAlign: 'right' }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <input type="hidden" name="blocks" value={3} />
        <div className="two-col" style={{ marginTop: 14 }}>
          <label>
            Standing charge / day
            <input name="standing" type="number" step="0.01" defaultValue="0" style={{ width: '100%', marginTop: 4 }} />
          </label>
          <label>
            Duty / tax %
            <input name="tax" type="number" step="0.01" defaultValue="0" style={{ width: '100%', marginTop: 4 }} />
          </label>
          <label>
            Billing period starts on day
            <input name="start_day" type="number" min="1" max="28" defaultValue="1" style={{ width: '100%', marginTop: 4 }} />
          </label>
        </div>
        <div style={{ marginTop: 16, display: 'flex', gap: 10 }}>
          <button className="action" type="submit">
            Save tariff
          </button>
          <button className="action" type="button" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </form>
    </Panel>
  )
}

export function PeriodCard({ summary, currency }) {
  const period = summary?.energy_this_period
  if (!period) return null
  return (
    <Card
      label="Energy this period"
      value={num(period.energy_kwh, 1)}
      unit="kWh"
      hint={
        period.days_skipped
          ? `${period.days_metered} days counted, ${period.days_skipped} excluded for gaps.`
          : `Across ${period.days_metered} fully covered days.`
      }
      tag={period.cost !== null && period.cost !== undefined ? null : null}
    />
  )
}

export { money }
