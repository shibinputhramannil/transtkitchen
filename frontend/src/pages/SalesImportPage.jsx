import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  applySalesImport,
  discardSalesImport,
  errorMessages,
  getOrganization,
  getSalesImport,
  listAllMenuItems,
  listSalesImportRows,
  mapSalesImportItem,
  rematchSalesImport,
} from '../api/client.js'
import FormErrors from '../components/FormErrors.jsx'
import { formatQuantity } from '../format.js'

// Everything from the file (item names, errors) is rendered as plain text by React.

function UnmatchedName({ organizationId, importId, entry, menuItems, onPreview }) {
  const [choice, setChoice] = useState('')
  const [errors, setErrors] = useState([])
  const [busy, setBusy] = useState(false)

  async function submit(menuItemId) {
    if (busy) return
    setBusy(true)
    setErrors([])
    try {
      onPreview(await mapSalesImportItem(organizationId, importId, entry.item_name, menuItemId))
    } catch (error) {
      setErrors(errorMessages(error))
      setBusy(false)
    }
  }

  return (
    <li>
      <div className="ingredient-summary">
        <strong>{entry.item_name}</strong>
        <span className="muted">
          {entry.rows} rows · {formatQuantity(entry.quantity)} sold
        </span>
      </div>
      <div className="toolbar">
        <select aria-label={`Menu item for ${entry.item_name}`} value={choice} onChange={(event) => setChoice(event.target.value)}>
          <option value="">Choose a menu item…</option>
          {menuItems.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
        <button type="button" disabled={!choice || busy} onClick={() => submit(choice)}>
          Map
        </button>
        <button type="button" disabled={busy} onClick={() => submit(null)}>
          Ignore in this import
        </button>
      </div>
      <FormErrors errors={errors} />
    </li>
  )
}

function ErrorRows({ organizationId, importId, count }) {
  const [rows, setRows] = useState(null)

  useEffect(() => {
    let cancelled = false
    listSalesImportRows(organizationId, importId, 'error').then((data) => {
      if (!cancelled) setRows(data)
    })
    return () => {
      cancelled = true
    }
  }, [organizationId, importId, count])

  if (!rows) return null
  return (
    <ul className="batch-list">
      {rows.results.map((row) => (
        <li key={row.row_number}>
          <span>
            <strong>Row {row.row_number}</strong>
            {row.item_name && <span className="muted"> · {row.item_name}</span>}
          </span>
          <span className="error">{row.error}</span>
        </li>
      ))}
      {rows.next && <li className="muted">Showing the first {rows.results.length} of {rows.count}.</li>}
    </ul>
  )
}

function Warning({ warning, checked, onToggle, disabled }) {
  return (
    <li className="warning">
      <p>{warning.message}</p>
      {warning.code === 'shortfalls' && (
        <ul>
          {warning.details.map((item) => (
            <li key={item.ingredient.id}>
              {item.ingredient.name}: short by {formatQuantity(item.shortfall)} {item.ingredient.unit}
            </li>
          ))}
        </ul>
      )}
      {warning.code === 'overlapping_dates' && <p className="muted">Dates: {warning.details.join(', ')}</p>}
      {warning.code === 'counted_after_sales' && (
        <ul>
          {warning.details.map((item) => (
            <li key={item.ingredient.id}>
              {item.ingredient.name}: counted {new Date(item.counted_at).toLocaleString()}, earliest
              sale {new Date(item.earliest_sale).toLocaleString()}
            </li>
          ))}
        </ul>
      )}
      <label className="checkbox">
        <input type="checkbox" checked={checked} disabled={disabled} onChange={onToggle} />
        I understand; apply anyway
      </label>
    </li>
  )
}

export default function SalesImportPage() {
  const { organizationId, importId } = useParams()
  const [organization, setOrganization] = useState(null)
  const [preview, setPreview] = useState(null)
  const [menuItems, setMenuItems] = useState([])
  const [acknowledged, setAcknowledged] = useState([])
  const [errors, setErrors] = useState([])
  const [applying, setApplying] = useState(false)
  const [rematching, setRematching] = useState(false)
  const inFlight = useRef(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      getOrganization(organizationId),
      getSalesImport(organizationId, importId),
      listAllMenuItems(organizationId),
    ]).then(
      ([org, data, items]) => {
        if (!cancelled) {
          setOrganization(org)
          setPreview(data)
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
  }, [organizationId, importId])

  if (!preview || !organization) {
    return (
      <section className="card">
        {errors.length ? <FormErrors errors={errors} /> : <p>Loading…</p>}
      </section>
    )
  }

  const canManage = organization.role === 'owner' || organization.role === 'manager'
  const isDraft = preview.status === 'draft'
  const allAcknowledged = preview.warnings.every((warning) => acknowledged.includes(warning.code))

  function toggle(code) {
    setAcknowledged((current) => (current.includes(code) ? current.filter((c) => c !== code) : [...current, code]))
  }

  async function handleApply() {
    if (inFlight.current) return
    inFlight.current = true
    setApplying(true)
    setErrors([])
    try {
      // Applying again after a lost response returns the stored result; nothing is deducted twice.
      setPreview(await applySalesImport(organizationId, importId, acknowledged))
    } catch (error) {
      if (error.data?.preview) {
        // Stock or sales changed since the preview: show the new one.
        setPreview((current) => ({ ...current, ...error.data.preview }))
      }
      setErrors(
        !error.status || error.status >= 500
          ? ['The server could not be reached. Applying again is safe; nothing will be deducted twice.']
          : errorMessages(error),
      )
    }
    inFlight.current = false
    setApplying(false)
  }

  async function handleRematch() {
    if (rematching) return
    setRematching(true)
    setErrors([])
    try {
      setPreview(await rematchSalesImport(organizationId, importId))
    } catch (error) {
      setErrors(errorMessages(error))
    }
    setRematching(false)
  }

  async function handleDiscard() {
    try {
      const discarded = await discardSalesImport(organizationId, importId)
      setPreview((current) => ({ ...current, ...discarded, can_apply: false }))
    } catch (error) {
      setErrors(errorMessages(error))
    }
  }

  const { summary } = preview

  return (
    <section className="card">
      <div className="card-header">
        <h2>{preview.file_name || 'Sales import'}</h2>
        <Link to={`/organizations/${organizationId}/sales`}>All imports</Link>
      </div>
      <p>
        <span className={`badge ${preview.status === 'applied' ? 'badge-ok' : ''}`}>{preview.status}</span>{' '}
        <span className="muted">
          {preview.first_date ?? '?'} to {preview.last_date ?? '?'} · {summary.rows} rows ·{' '}
          {summary.matched_rows} matched · {summary.unmatched_rows} unmatched · {summary.error_rows} with
          errors · {summary.ignored_rows} ignored
        </span>
      </p>
      {preview.duplicate && <p className="muted">This file was uploaded before; this is the same draft.</p>}
      {preview.status === 'applied' && (
        <p>
          Applied {new Date(preview.applied_at).toLocaleString()} by {preview.applied_by}.
        </p>
      )}

      {isDraft && canManage && preview.rematch_available > 0 && (
        <div className="toolbar">
          <span className="muted">
            {preview.rematch_available === 1 ? '1 row' : `${preview.rematch_available} rows`} would match
            differently with the current menu and remembered names.
          </span>
          <button type="button" disabled={rematching} onClick={handleRematch}>
            {rematching ? 'Re-matching…' : 'Re-match names'}
          </button>
        </div>
      )}

      {preview.blocking.length > 0 && (
        <div className="form-errors" role="alert">
          {preview.blocking.map((item) => (
            <p key={item.code}>{item.message}</p>
          ))}
        </div>
      )}

      {preview.unmatched.length > 0 && (
        <>
          <h3>Unmatched item names</h3>
          {canManage && isDraft ? (
            <ul className="ingredient-list">
              {preview.unmatched.map((entry) => (
                <UnmatchedName
                  key={entry.item_name}
                  organizationId={organizationId}
                  importId={importId}
                  entry={entry}
                  menuItems={menuItems}
                  onPreview={setPreview}
                />
              ))}
            </ul>
          ) : (
            <ul>
              {preview.unmatched.map((entry) => (
                <li key={entry.item_name}>{entry.item_name}</li>
              ))}
            </ul>
          )}
        </>
      )}

      {summary.error_rows > 0 && (
        <>
          <h3>Rows with errors</h3>
          <ErrorRows organizationId={organizationId} importId={importId} count={summary.error_rows} />
        </>
      )}

      <h3>Items sold</h3>
      {preview.items.length === 0 ? (
        <p className="muted">No matched items.</p>
      ) : (
        <ul className="batch-list">
          {preview.items.map((item) => (
            <li key={item.menu_item.id} className="ingredient-summary">
              <span>
                {item.menu_item.name}
                {!item.has_recipe && <span className="muted"> · no recipe, no stock deducted</span>}
              </span>
              <strong>{formatQuantity(item.quantity)}</strong>
            </li>
          ))}
        </ul>
      )}

      <h3>{preview.status === 'applied' ? 'Stock deducted' : 'Stock to deduct'}</h3>
      {preview.deductions.length === 0 ? (
        <p className="muted">Nothing to deduct.</p>
      ) : (
        <ul className="batch-list">
          {preview.deductions.map((item) => (
            <li key={item.ingredient.id} className="ingredient-summary">
              <span>
                {item.ingredient.name}{' '}
                <span className="muted">
                  · needs {formatQuantity(item.required)} {item.ingredient.unit}
                  {item.on_hand !== undefined && <> · {formatQuantity(item.on_hand)} on hand</>}
                  {item.taken !== undefined && <> · took {formatQuantity(item.taken)}</>}
                </span>
              </span>
              {Number(item.shortfall) > 0 && (
                <span className="badge badge-error">
                  short {formatQuantity(item.shortfall)} {item.ingredient.unit}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}

      {isDraft && preview.warnings.length > 0 && (
        <>
          <h3>Check before applying</h3>
          <ul className="warning-list">
            {preview.warnings.map((warning) => (
              <Warning
                key={warning.code}
                warning={warning}
                checked={acknowledged.includes(warning.code)}
                disabled={!canManage}
                onToggle={() => toggle(warning.code)}
              />
            ))}
          </ul>
        </>
      )}

      <FormErrors errors={errors} />
      {isDraft && canManage && (
        <div className="actions">
          <button
            type="button"
            className="button-primary"
            disabled={!preview.can_apply || !allAcknowledged || applying}
            onClick={handleApply}
          >
            {applying ? 'Applying…' : 'Apply import'}
          </button>
          <button type="button" className="button-danger" disabled={applying} onClick={handleDiscard}>
            Discard
          </button>
        </div>
      )}
    </section>
  )
}
