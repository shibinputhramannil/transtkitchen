import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  errorMessages,
  getOrganization,
  getWasteSummary,
  listAllMenuItems,
  listWaste,
  voidWaste,
} from '../api/client.js'
import FormErrors from '../components/FormErrors.jsx'
import WasteForm from '../components/WasteForm.jsx'
import { WASTE_REASONS, formatQuantity } from '../format.js'

function quantityText(entry) {
  return entry.kind === 'menu_item'
    ? `${formatQuantity(entry.quantity)} portions of ${entry.menu_item.name}`
    : `${formatQuantity(entry.quantity)} ${entry.unit} of ${entry.ingredient.name}`
}

function VoidForm({ organizationId, entry, onVoided, onCancel }) {
  const [reason, setReason] = useState('')
  const [errors, setErrors] = useState([])
  const [busy, setBusy] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setErrors([])
    try {
      // Voiding again changes nothing, so a retry is safe.
      onVoided(await voidWaste(organizationId, entry.id, reason))
    } catch (error) {
      setErrors(errorMessages(error))
      setBusy(false)
    }
  }

  return (
    <form className="toolbar" onSubmit={handleSubmit}>
      <input
        aria-label="Why void this entry"
        placeholder="Why void it? (e.g. typed 50 instead of 5)"
        required
        maxLength={255}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
      />
      <button type="submit" className="button-danger" disabled={busy}>
        Void and return stock
      </button>
      <button type="button" onClick={onCancel} disabled={busy}>
        Cancel
      </button>
      <FormErrors errors={errors} />
    </form>
  )
}

function Entry({ organizationId, entry, canVoid, onChanged }) {
  const [voiding, setVoiding] = useState(false)
  const voided = Boolean(entry.voided_at)

  return (
    <li>
      <div className="ingredient-summary">
        <span className={voided ? 'voided' : undefined}>
          <strong>{quantityText(entry)}</strong>
          <span className="muted">
            {' '}
            · {WASTE_REASONS[entry.reason]} · {entry.wasted_on}
            {entry.batch && <> · from the batch {entry.batch.expires_on ? `expiring ${entry.batch.expires_on}` : 'with no expiry'}</>}
          </span>
        </span>
        {voided && <span className="badge">Voided</span>}
      </div>
      {entry.note && <span className="muted">{entry.note}</span>}
      {entry.kind === 'menu_item' && (
        <span className="muted">
          {entry.deductions.length
            ? `Used ${entry.deductions
                .map((d) => `${formatQuantity(d.required)} ${d.unit} ${d.ingredient.name}`)
                .join(', ')}`
            : 'No recipe: no stock deducted.'}
        </span>
      )}
      {entry.deductions.some((d) => Number(d.shortfall) > 0) && (
        <span className="muted">
          Shortfall:{' '}
          {entry.deductions
            .filter((d) => Number(d.shortfall) > 0)
            .map((d) => `${formatQuantity(d.shortfall)} ${d.unit} ${d.ingredient.name}`)
            .join(', ')}
        </span>
      )}
      <span className="muted">
        Logged by {entry.created_by ?? 'a former member'}
        {voided && <> · voided by {entry.voided_by ?? 'a former member'}: {entry.void_reason}</>}
      </span>
      {canVoid && !voided && !voiding && (
        <div className="actions">
          <button type="button" onClick={() => setVoiding(true)}>
            Void
          </button>
        </div>
      )}
      {voiding && (
        <VoidForm
          organizationId={organizationId}
          entry={entry}
          onVoided={() => {
            setVoiding(false)
            onChanged()
          }}
          onCancel={() => setVoiding(false)}
        />
      )}
    </li>
  )
}

function Summary({ summary }) {
  return (
    <section aria-labelledby="summary-heading">
      <h3 id="summary-heading">
        Waste {summary.from} to {summary.to}
      </h3>
      <p className="muted">
        {summary.entries === 1 ? '1 entry' : `${summary.entries} entries`}, voided entries left out. Quantities are in each ingredient&apos;s stock
        unit and never added across units.
      </p>
      {summary.entries === 0 ? (
        <p className="muted">No waste logged in this period.</p>
      ) : (
        <>
          <h4>By ingredient</h4>
          <ul className="batch-list">
            {summary.by_ingredient.map((row) => (
              <li key={`${row.ingredient.id}-${row.unit}`} className="ingredient-summary">
                <span>{row.ingredient.name}</span>
                <strong>
                  {formatQuantity(row.quantity)} {row.unit}
                </strong>
              </li>
            ))}
          </ul>
          <h4>By reason</h4>
          <ul className="batch-list">
            {summary.by_reason.map((row) => (
              <li key={row.reason} className="ingredient-summary">
                <span>
                  {WASTE_REASONS[row.reason]}{' '}
                  <span className="muted">· {row.entries === 1 ? '1 entry' : `${row.entries} entries`}</span>
                </span>
                <span>
                  {[
                    ...row.quantities.map((q) => `${formatQuantity(q.quantity)} ${q.unit}`),
                    ...(Number(row.portions) > 0 ? [`${formatQuantity(row.portions)} portions`] : []),
                  ].join(' · ')}
                </span>
              </li>
            ))}
          </ul>
          {summary.by_menu_item.length > 0 && (
            <>
              <h4>Menu items</h4>
              <ul className="batch-list">
                {summary.by_menu_item.map((row) => (
                  <li key={row.menu_item.id} className="ingredient-summary">
                    <span>{row.menu_item.name}</span>
                    <strong>{formatQuantity(row.portions)} portions</strong>
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </section>
  )
}

export default function WastePage() {
  const { organizationId } = useParams()
  const [organization, setOrganization] = useState(null)
  const [menuItems, setMenuItems] = useState([])
  const [entries, setEntries] = useState(null)
  const [summary, setSummary] = useState(null)
  const [includeVoided, setIncludeVoided] = useState(false)
  const [logging, setLogging] = useState(false)
  const [errors, setErrors] = useState([])
  const [reloadCount, setReloadCount] = useState(0)
  const reload = () => setReloadCount((count) => count + 1)

  useEffect(() => {
    let cancelled = false
    Promise.all([getOrganization(organizationId), listAllMenuItems(organizationId)]).then(
      ([org, items]) => {
        if (!cancelled) {
          setOrganization(org)
          setMenuItems(items)
        }
      },
      (error) => {
        if (!cancelled) setErrors(errorMessages(error))
      },
    )
    return () => {
      cancelled = true
    }
  }, [organizationId])

  useEffect(() => {
    let cancelled = false
    Promise.all([listWaste(organizationId, { includeVoided }), getWasteSummary(organizationId)]).then(
      ([list, totals]) => {
        if (!cancelled) {
          setEntries({ includeVoided, ...list })
          setSummary(totals)
        }
      },
      (error) => {
        if (!cancelled) setErrors(errorMessages(error))
      },
    )
    return () => {
      cancelled = true
    }
  }, [organizationId, includeVoided, reloadCount])

  if (!organization) {
    return (
      <section className="card">
        {errors.length ? <FormErrors errors={errors} /> : <p>Loading…</p>}
      </section>
    )
  }
  const canVoid = organization.role === 'owner' || organization.role === 'manager'

  return (
    <section className="card">
      <div className="card-header">
        <h2>Waste · {organization.name}</h2>
        <Link to="/" state={{ organizationId }}>
          Dashboard
        </Link>
      </div>
      <p className="muted">
        Log ingredient waste and discard batches from the Inventory page. Log prepared dishes here, in
        portions.
      </p>
      {logging ? (
        <WasteForm
          organizationId={organizationId}
          timeZone={organization.time_zone}
          menuItems={menuItems}
          initialReason="overproduction"
          onSaved={() => {
            setLogging(false)
            reload()
          }}
          onCancel={() => setLogging(false)}
        />
      ) : (
        <button type="button" onClick={() => setLogging(true)}>
          Log menu-item waste
        </button>
      )}
      <FormErrors errors={errors} />

      <h3>Recent waste</h3>
      <label className="checkbox">
        <input type="checkbox" checked={includeVoided} onChange={(event) => setIncludeVoided(event.target.checked)} />
        Show voided entries
      </label>
      {!entries || entries.includeVoided !== includeVoided ? (
        <p>Loading…</p>
      ) : entries.results.length === 0 ? (
        <p className="muted">No waste logged yet.</p>
      ) : (
        <ul className="ingredient-list">
          {entries.results.map((entry) => (
            <Entry
              key={entry.id}
              organizationId={organizationId}
              entry={entry}
              canVoid={canVoid}
              onChanged={reload}
            />
          ))}
        </ul>
      )}
      {entries?.next && <p className="muted">Showing the latest {entries.results.length} of {entries.count}.</p>}

      {summary && <Summary summary={summary} />}
    </section>
  )
}
