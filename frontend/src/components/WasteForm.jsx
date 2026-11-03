import { useRef, useState } from 'react'
import { errorMessages, logWaste } from '../api/client.js'
import { WASTE_REASONS, compatibleUnits, formatQuantity } from '../format.js'
import { addDays, localDate } from '../timeZones.js'
import FormErrors from './FormErrors.jsx'

// Waste can be logged up to this many days back (the server checks too).
const MAX_DAYS_BACK = 7

// Logs waste of an ingredient (optionally from one batch) or, with
// `menuItems`, portions of a menu item. The request ID is chosen when the
// form opens and kept for every retry, so the waste is never logged twice.
export default function WasteForm({
  organizationId,
  timeZone,
  ingredient,
  batch,
  menuItems,
  initialQuantity = '',
  initialReason = 'spoiled',
  onSaved,
  onCancel,
}) {
  const [requestId] = useState(() => crypto.randomUUID())
  const today = localDate(timeZone)
  const [menuItem, setMenuItem] = useState('')
  const [quantity, setQuantity] = useState(initialQuantity)
  const [unit, setUnit] = useState(ingredient?.unit ?? 'each')
  const [reason, setReason] = useState(initialReason)
  const [wastedOn, setWastedOn] = useState(today)
  const [note, setNote] = useState('')
  const [warnings, setWarnings] = useState([])
  const [acknowledged, setAcknowledged] = useState([])
  const [errors, setErrors] = useState([])
  const [submitting, setSubmitting] = useState(false)
  const inFlight = useRef(false)

  const isMenuItem = Boolean(menuItems)

  async function handleSubmit(event) {
    event.preventDefault()
    if (inFlight.current) return
    inFlight.current = true
    setSubmitting(true)
    setErrors([])
    const entry = {
      request_id: requestId,
      quantity,
      reason,
      wasted_on: wastedOn,
      note,
      acknowledged,
      ...(isMenuItem
        ? { menu_item: menuItem, unit: 'each' }
        : { ingredient: ingredient.id, unit, ...(batch ? { batch: batch.id } : {}) }),
    }
    try {
      onSaved(await logWaste(organizationId, entry))
    } catch (error) {
      if (error.status === 409 && error.data?.warnings) {
        // Nothing was logged; confirm the warnings and submit again.
        setWarnings(error.data.warnings)
        setErrors([])
      } else {
        setErrors(
          !error.status || error.status >= 500
            ? ['Not saved: the server could not be reached. Submitting again is safe; it will not be logged twice.']
            : errorMessages(error),
        )
      }
      inFlight.current = false
      setSubmitting(false)
    }
  }

  const unacknowledged = warnings.some((warning) => !acknowledged.includes(warning.code))

  return (
    <form className="form inline-form" onSubmit={handleSubmit}>
      {batch && (
        <p className="muted">
          From the batch {batch.expires_on ? `expiring ${batch.expires_on}` : 'with no expiry date'}, which
          holds {formatQuantity(batch.quantity_remaining)} {ingredient.unit}.
        </p>
      )}
      <div className="form-row">
        {isMenuItem && (
          <label>
            Menu item
            <select required value={menuItem} onChange={(event) => setMenuItem(event.target.value)}>
              <option value="">Choose…</option>
              {menuItems.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          {isMenuItem ? 'Portions' : 'Quantity'}
          <input
            type="number"
            inputMode="decimal"
            required
            min="0.001"
            step="0.001"
            value={quantity}
            onChange={(event) => setQuantity(event.target.value)}
          />
        </label>
        {!isMenuItem && (
          <label>
            Unit
            <select value={unit} onChange={(event) => setUnit(event.target.value)}>
              {compatibleUnits(ingredient.unit).map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          Reason
          <select value={reason} onChange={(event) => setReason(event.target.value)}>
            {Object.entries(WASTE_REASONS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Date
          <input
            type="date"
            required
            min={addDays(today, -MAX_DAYS_BACK)}
            max={today}
            value={wastedOn}
            onChange={(event) => setWastedOn(event.target.value)}
          />
        </label>
        <label>
          Note
          <input maxLength={255} value={note} onChange={(event) => setNote(event.target.value)} />
        </label>
      </div>
      {warnings.length > 0 && (
        <ul className="warning-list">
          {warnings.map((warning) => (
            <li key={warning.code} className="warning">
              <p>{warning.message}</p>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={acknowledged.includes(warning.code)}
                  onChange={() =>
                    setAcknowledged((current) =>
                      current.includes(warning.code)
                        ? current.filter((code) => code !== warning.code)
                        : [...current, warning.code],
                    )
                  }
                />
                I understand; log it anyway
              </label>
            </li>
          ))}
        </ul>
      )}
      <FormErrors errors={errors} />
      <div className="actions">
        <button type="submit" disabled={submitting || unacknowledged}>
          {submitting ? 'Saving…' : 'Log waste'}
        </button>
        <button type="button" onClick={onCancel} disabled={submitting}>
          Cancel
        </button>
      </div>
    </form>
  )
}
