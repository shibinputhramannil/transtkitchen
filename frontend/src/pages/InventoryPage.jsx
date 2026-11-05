import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  correctBatchExpiry,
  countStock,
  createIngredient,
  errorMessages,
  getOrganization,
  listBatches,
  listExpiring,
  listIngredients,
  listStockMovements,
  receiveStock,
  updateIngredient,
} from '../api/client.js'
import FormErrors from '../components/FormErrors.jsx'
import WasteForm from '../components/WasteForm.jsx'
import { formatQuantity } from '../format.js'
import { localDate } from '../timeZones.js'

const UNITS = ['g', 'kg', 'ml', 'l', 'each']
const SOURCE_LABELS = {
  delivery: 'Delivery',
  count: 'Found in a count',
  opening: 'Stock from before batches',
}

// Expiry statuses come from the server, which uses the kitchen's local date.
function ExpiryBadge({ status, expiresOn }) {
  if (status === 'expired') return <span className="badge badge-error">Expired {expiresOn}</span>
  if (status === 'expiring') return <span className="badge badge-warning">Expires {expiresOn}</span>
  if (status === 'ok') return <span className="badge">Expires {expiresOn}</span>
  return <span className="badge">No expiry recorded</span>
}

function IngredientForm({ initial, submitLabel, onSubmit, onCancel }) {
  const [values, setValues] = useState(initial)
  const [errors, setErrors] = useState([])
  const [submitting, setSubmitting] = useState(false)

  function update(field) {
    return (event) => setValues((current) => ({ ...current, [field]: event.target.value }))
  }

  async function handleSubmit(event) {
    event.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setErrors([])
    try {
      await onSubmit({
        name: values.name,
        unit: values.unit,
        reorder_level: values.reorderLevel === '' ? null : values.reorderLevel,
      })
    } catch (error) {
      setErrors(errorMessages(error))
      setSubmitting(false)
    }
  }

  return (
    <form className="form inline-form" onSubmit={handleSubmit}>
      <div className="form-row">
        <label>
          Name
          <input required maxLength={120} value={values.name} onChange={update('name')} />
        </label>
        <label>
          Unit
          <select value={values.unit} onChange={update('unit')}>
            {UNITS.map((unit) => (
              <option key={unit} value={unit}>
                {unit}
              </option>
            ))}
          </select>
        </label>
        <label>
          Reorder level
          <input
            type="number"
            inputMode="decimal"
            min="0"
            step="0.001"
            value={values.reorderLevel}
            onChange={update('reorderLevel')}
          />
        </label>
      </div>
      <FormErrors errors={errors} />
      <div className="actions">
        <button type="submit" disabled={submitting}>
          {submitting ? 'Saving…' : submitLabel}
        </button>
        {onCancel && (
          <button type="button" onClick={onCancel} disabled={submitting}>
            Cancel
          </button>
        )}
      </div>
    </form>
  )
}

// Records a delivery or a count. The request ID is chosen when the form opens
// and kept for every retry, so submitting again after an error or a lost
// response can never apply the change twice.
function StockForm({ organizationId, timeZone, ingredient, kind, onSaved, onConflict, onCancel }) {
  const [requestId] = useState(() => crypto.randomUUID())
  // A count is checked against the stock level the counter started from.
  const [expectedQuantity] = useState(ingredient.quantity_on_hand)
  const [quantity, setQuantity] = useState('')
  const [expiresOn, setExpiresOn] = useState('')
  const [note, setNote] = useState('')
  const [errors, setErrors] = useState([])
  const [submitting, setSubmitting] = useState(false)
  const inFlight = useRef(false)

  const isCount = kind === 'count'

  async function handleSubmit(event) {
    event.preventDefault()
    if (inFlight.current) return
    inFlight.current = true
    setSubmitting(true)
    setErrors([])
    try {
      const movement = isCount
        ? await countStock(organizationId, ingredient.id, {
            countedQuantity: quantity,
            expectedQuantity,
            note,
            requestId,
          })
        : await receiveStock(organizationId, ingredient.id, { quantity, expiresOn, note, requestId })
      onSaved(movement)
    } catch (error) {
      if (error.status === 409) {
        onConflict()
      }
      setErrors(
        !error.status || error.status >= 500
          ? ['Not saved: the server could not be reached. Submitting again is safe; it will not be recorded twice.']
          : errorMessages(error),
      )
      inFlight.current = false
      setSubmitting(false)
    }
  }

  return (
    <form className="form inline-form" onSubmit={handleSubmit}>
      <div className="form-row">
        <label>
          {isCount ? `Counted quantity (${ingredient.unit})` : `Quantity received (${ingredient.unit})`}
          <input
            type="number"
            inputMode="decimal"
            required
            min={isCount ? '0' : '0.001'}
            step="0.001"
            value={quantity}
            onChange={(event) => setQuantity(event.target.value)}
          />
        </label>
        {!isCount && (
          <label>
            Expiry date (optional)
            <input
              type="date"
              min={localDate(timeZone)}
              value={expiresOn}
              onChange={(event) => setExpiresOn(event.target.value)}
            />
          </label>
        )}
        <label>
          Note
          <input maxLength={255} value={note} onChange={(event) => setNote(event.target.value)} />
        </label>
      </div>
      <FormErrors errors={errors} />
      <div className="actions">
        <button type="submit" disabled={submitting}>
          {submitting ? 'Saving…' : isCount ? 'Save count' : 'Add to stock'}
        </button>
        <button type="button" onClick={onCancel} disabled={submitting}>
          Cancel
        </button>
      </div>
    </form>
  )
}

function ExpiryCorrectionForm({ organizationId, ingredientId, batch, onSaved, onCancel }) {
  const [expiresOn, setExpiresOn] = useState(batch.expires_on ?? '')
  const [note, setNote] = useState('')
  const [errors, setErrors] = useState([])
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setErrors([])
    try {
      // An empty date records that the batch has no expiry date. Repeating a
      // correction is harmless: the same date changes nothing.
      await correctBatchExpiry(organizationId, ingredientId, batch.id, {
        expiresOn: expiresOn || null,
        note,
      })
      onSaved()
    } catch (error) {
      setErrors(errorMessages(error))
      setSubmitting(false)
    }
  }

  return (
    <form className="form inline-form" onSubmit={handleSubmit}>
      <div className="form-row">
        <label>
          Expiry date (empty for none)
          <input type="date" value={expiresOn} onChange={(event) => setExpiresOn(event.target.value)} />
        </label>
        <label>
          Reason
          <input maxLength={200} value={note} onChange={(event) => setNote(event.target.value)} />
        </label>
      </div>
      <FormErrors errors={errors} />
      <div className="actions">
        <button type="submit" disabled={submitting}>
          {submitting ? 'Saving…' : 'Correct expiry date'}
        </button>
        <button type="button" onClick={onCancel} disabled={submitting}>
          Cancel
        </button>
      </div>
    </form>
  )
}

const MOVEMENT_LABELS = {
  receive: 'Delivery',
  count: 'Stock count',
  expiry_correction: 'Expiry date corrected',
  sale: 'Sales',
  waste: 'Waste',
  waste_void: 'Waste voided',
}

function signed(quantity) {
  return Number(quantity) > 0 ? `+${formatQuantity(quantity)}` : formatQuantity(quantity)
}

// Every change to the ingredient's stock, newest first, with the batches each one touched.
function StockHistory({ organizationId, ingredient }) {
  const [page, setPage] = useState(1)
  const [result, setResult] = useState(null)
  const [errors, setErrors] = useState([])

  useEffect(() => {
    let cancelled = false
    listStockMovements(organizationId, ingredient.id, page).then(
      (data) => {
        if (!cancelled) setResult({ page, ...data })
      },
      (error) => {
        if (!cancelled) setErrors(errorMessages(error))
      },
    )
    return () => {
      cancelled = true
    }
  }, [organizationId, ingredient.id, ingredient.quantity_on_hand, page])

  if (errors.length) return <FormErrors errors={errors} />
  if (!result || result.page !== page) return <p>Loading history…</p>
  if (!result.results.length) return <p className="muted">No stock changes yet.</p>

  return (
    <div className="inline-form">
      <ul className="batch-list">
        {result.results.map((movement) => (
          <li key={movement.id}>
            <div className="ingredient-summary">
              <span>
                <strong>{MOVEMENT_LABELS[movement.kind] ?? movement.kind}</strong>
                <span className="muted">
                  {' '}
                  · {new Date(movement.created_at).toLocaleString()} · {movement.created_by ?? 'a former member'}
                </span>
              </span>
              <span>
                {movement.kind !== 'expiry_correction' && (
                  <strong>
                    {signed(movement.quantity_change)} {ingredient.unit}
                  </strong>
                )}
                <span className="muted">
                  {' '}
                  → {formatQuantity(movement.quantity_after)} {ingredient.unit}
                </span>
              </span>
            </div>
            {movement.note && <span className="muted">{movement.note}</span>}
            {movement.lines.length > 0 && (
              <span className="muted">
                Batches:{' '}
                {movement.lines
                  .map(
                    (line) =>
                      `${signed(line.quantity_change)} ${ingredient.unit} (${line.expires_on ? `expiring ${line.expires_on}` : 'no expiry'})`,
                  )
                  .join('; ')}
              </span>
            )}
          </li>
        ))}
      </ul>
      {(result.previous || result.next) && (
        <div className="actions">
          <button type="button" disabled={!result.previous} onClick={() => setPage((current) => current - 1)}>
            Newer
          </button>
          <span className="muted">Page {page}</span>
          <button type="button" disabled={!result.next} onClick={() => setPage((current) => current + 1)}>
            Older
          </button>
        </div>
      )}
    </div>
  )
}

// Throwing a whole batch out defaults to its full remaining quantity.
function discardReason(batch) {
  return batch.status === 'expired' ? 'expired' : 'spoiled'
}

function BatchList({ organizationId, timeZone, ingredient, canManage, onChanged }) {
  const [result, setResult] = useState(null)
  const [errors, setErrors] = useState([])
  const [correcting, setCorrecting] = useState(null)
  const [discarding, setDiscarding] = useState(null)
  const [reloadCount, setReloadCount] = useState(0)

  useEffect(() => {
    let cancelled = false
    listBatches(organizationId, ingredient.id).then(
      (data) => {
        if (!cancelled) setResult(data)
      },
      (error) => {
        if (!cancelled) setErrors(errorMessages(error))
      },
    )
    return () => {
      cancelled = true
    }
  }, [organizationId, ingredient.id, ingredient.quantity_on_hand, reloadCount])

  if (errors.length) return <FormErrors errors={errors} />
  if (!result) return <p>Loading batches…</p>
  if (!result.results.length) return <p className="muted">No stock in any batch.</p>

  return (
    <ul className="batch-list">
      {result.results.map((batch) => (
        <li key={batch.id}>
          <div className="ingredient-summary">
            <span>
              <strong>
                {formatQuantity(batch.quantity_remaining)} {ingredient.unit}
              </strong>
              <span className="muted">
                {' '}
                of {formatQuantity(batch.quantity_received)} · {SOURCE_LABELS[batch.source]} ·{' '}
                {new Date(batch.received_at).toLocaleDateString()}
              </span>
            </span>
            <ExpiryBadge status={batch.status} expiresOn={batch.expires_on} />
          </div>
          {correcting !== batch.id && discarding !== batch.id && (
            <div className="actions">
              {ingredient.is_active && (
                <button type="button" onClick={() => setDiscarding(batch.id)}>
                  Discard
                </button>
              )}
              {canManage && (
                <button type="button" onClick={() => setCorrecting(batch.id)}>
                  Correct expiry date
                </button>
              )}
            </div>
          )}
          {discarding === batch.id && (
            <WasteForm
              organizationId={organizationId}
              timeZone={timeZone}
              ingredient={ingredient}
              batch={batch}
              initialQuantity={batch.quantity_remaining}
              initialReason={discardReason(batch)}
              onSaved={() => {
                setDiscarding(null)
                setReloadCount((count) => count + 1)
                onChanged()
              }}
              onCancel={() => setDiscarding(null)}
            />
          )}
          {correcting === batch.id && (
            <ExpiryCorrectionForm
              organizationId={organizationId}
              ingredientId={ingredient.id}
              batch={batch}
              onSaved={() => {
                setCorrecting(null)
                setReloadCount((count) => count + 1)
                onChanged()
              }}
              onCancel={() => setCorrecting(null)}
            />
          )}
        </li>
      ))}
    </ul>
  )
}

function IngredientRow({ organizationId, timeZone, ingredient, canManage, onChanged, onReload }) {
  // null, 'receive', 'count', 'waste', 'edit', 'batches', or 'history'.
  const [open, setOpen] = useState(null)
  const [archiveErrors, setArchiveErrors] = useState([])
  const [archiving, setArchiving] = useState(false)

  async function handleArchive() {
    if (archiving) return
    setArchiving(true)
    setArchiveErrors([])
    try {
      await updateIngredient(organizationId, ingredient.id, { is_active: !ingredient.is_active })
      // It now belongs to the other list (active or archived).
      onReload()
    } catch (error) {
      setArchiveErrors(errorMessages(error))
    }
    setArchiving(false)
  }

  return (
    <li>
      <div className="ingredient-summary">
        <span className="ingredient-name">
          {ingredient.name}
          {ingredient.is_low_stock && <span className="badge badge-error">Low stock</span>}
        </span>
        <span>
          <strong>
            {formatQuantity(ingredient.quantity_on_hand)} {ingredient.unit}
          </strong>
          {ingredient.reorder_level !== null && (
            <span className="muted">
              {' '}
              · reorder at {formatQuantity(ingredient.reorder_level)} {ingredient.unit}
            </span>
          )}
        </span>
      </div>
      {ingredient.next_expiry && (
        <div className="ingredient-name">
          {Number(ingredient.expired_quantity) > 0 && (
            <span className="badge badge-error">
              {formatQuantity(ingredient.expired_quantity)} {ingredient.unit} expired
            </span>
          )}
          {ingredient.expiry_status !== 'expired' && (
            <ExpiryBadge status={ingredient.expiry_status} expiresOn={ingredient.next_expiry} />
          )}
        </div>
      )}
      {(open === null || open === 'batches' || open === 'history') && (
        <div className="actions">
          {open === null && ingredient.is_active && (
            <>
              <button type="button" onClick={() => setOpen('receive')}>
                Receive
              </button>
              <button type="button" onClick={() => setOpen('count')}>
                Count
              </button>
              <button type="button" onClick={() => setOpen('waste')}>
                Waste
              </button>
            </>
          )}
          <button
            type="button"
            aria-expanded={open === 'batches'}
            onClick={() => setOpen(open === 'batches' ? null : 'batches')}
          >
            {open === 'batches' ? 'Hide batches' : 'Batches'}
          </button>
          <button
            type="button"
            aria-expanded={open === 'history'}
            onClick={() => setOpen(open === 'history' ? null : 'history')}
          >
            {open === 'history' ? 'Hide history' : 'History'}
          </button>
          {open === null && canManage && (
            <>
              <button type="button" onClick={() => setOpen('edit')}>
                Edit
              </button>
              <button type="button" onClick={handleArchive} disabled={archiving}>
                {ingredient.is_active ? 'Archive' : 'Restore'}
              </button>
            </>
          )}
        </div>
      )}
      {open === 'batches' && (
        <BatchList
          organizationId={organizationId}
          timeZone={timeZone}
          ingredient={ingredient}
          canManage={canManage}
          onChanged={onReload}
        />
      )}
      {open === 'history' && <StockHistory organizationId={organizationId} ingredient={ingredient} />}
      <FormErrors errors={archiveErrors} />
      {open === 'waste' && (
        <WasteForm
          organizationId={organizationId}
          timeZone={timeZone}
          ingredient={ingredient}
          onSaved={() => {
            setOpen(null)
            onReload()
          }}
          onCancel={() => setOpen(null)}
        />
      )}
      {(open === 'receive' || open === 'count') && (
        <StockForm
          organizationId={organizationId}
          timeZone={timeZone}
          ingredient={ingredient}
          kind={open}
          onSaved={(movement) => {
            onChanged({ ...ingredient, quantity_on_hand: movement.quantity_after })
            setOpen(null)
            // The low-stock flag is computed by the server.
            onReload()
          }}
          onConflict={onReload}
          onCancel={() => setOpen(null)}
        />
      )}
      {open === 'edit' && (
        <IngredientForm
          initial={{
            name: ingredient.name,
            unit: ingredient.unit,
            reorderLevel: ingredient.reorder_level ?? '',
          }}
          submitLabel="Save changes"
          onSubmit={async (changes) => {
            onChanged(await updateIngredient(organizationId, ingredient.id, changes))
            setOpen(null)
          }}
          onCancel={() => setOpen(null)}
        />
      )}
    </li>
  )
}

function ExpiringSoon({ organizationId, timeZone, reloadKey, onChanged }) {
  const [result, setResult] = useState(null)
  const [errors, setErrors] = useState([])
  const [discarding, setDiscarding] = useState(null)

  useEffect(() => {
    let cancelled = false
    listExpiring(organizationId).then(
      (data) => {
        if (!cancelled) setResult(data)
      },
      (error) => {
        if (!cancelled) setErrors(errorMessages(error))
      },
    )
    return () => {
      cancelled = true
    }
  }, [organizationId, reloadKey])

  return (
    <section className="expiring" aria-labelledby="expiring-heading">
      <h3 id="expiring-heading">Expired or expiring within 3 days</h3>
      <FormErrors errors={errors} />
      {!result && !errors.length && <p>Loading…</p>}
      {result && !result.results.length && <p className="muted">Nothing is expiring soon.</p>}
      {result && result.results.length > 0 && (
        <ul className="batch-list">
          {result.results.map((batch) => (
            <li key={batch.id}>
              <div className="ingredient-summary">
                <span>
                  <strong>{batch.ingredient.name}</strong> · {formatQuantity(batch.quantity_remaining)}{' '}
                  {batch.ingredient.unit}
                </span>
                <span className="actions">
                  <ExpiryBadge status={batch.status} expiresOn={batch.expires_on} />
                  {batch.ingredient.is_active && discarding !== batch.id && (
                    <button type="button" onClick={() => setDiscarding(batch.id)}>
                      Discard
                    </button>
                  )}
                </span>
              </div>
              {discarding === batch.id && (
                <WasteForm
                  organizationId={organizationId}
                  timeZone={timeZone}
                  ingredient={batch.ingredient}
                  batch={batch}
                  initialQuantity={batch.quantity_remaining}
                  initialReason={discardReason(batch)}
                  onSaved={() => {
                    setDiscarding(null)
                    onChanged()
                  }}
                  onCancel={() => setDiscarding(null)}
                />
              )}
            </li>
          ))}
        </ul>
      )}
      {result?.next && <p className="muted">Showing the first {result.results.length} of {result.count}.</p>}
    </section>
  )
}

export default function InventoryPage() {
  const { organizationId } = useParams()
  const [organization, setOrganization] = useState(null)
  const [query, setQuery] = useState({ search: '', archived: false, page: 1 })
  const [searchInput, setSearchInput] = useState('')
  const [result, setResult] = useState(null)
  const [errors, setErrors] = useState([])
  const [adding, setAdding] = useState(false)
  const [reloadCount, setReloadCount] = useState(0)

  const reload = useCallback(() => setReloadCount((count) => count + 1), [])

  useEffect(() => {
    let cancelled = false
    getOrganization(organizationId).then(
      (data) => {
        if (!cancelled) setOrganization(data)
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
    listIngredients(organizationId, query).then(
      (data) => {
        if (!cancelled) setResult({ query, ...data })
      },
      (error) => {
        if (!cancelled) setErrors(errorMessages(error))
      },
    )
    return () => {
      cancelled = true
    }
  }, [organizationId, query, reloadCount])

  if (errors.length) {
    return (
      <section className="card">
        <FormErrors errors={errors} />
        <p>
          <Link to="/">Back to dashboard</Link>
        </p>
      </section>
    )
  }
  if (!organization) {
    return (
      <section className="card">
        <p>Loading…</p>
      </section>
    )
  }

  const canManage = organization.role === 'owner' || organization.role === 'manager'

  function replaceIngredient(updated) {
    setResult((current) => ({
      ...current,
      results: current.results.map((item) => (item.id === updated.id ? updated : item)),
    }))
  }

  function handleSearch(event) {
    event.preventDefault()
    setQuery((current) => ({ ...current, search: searchInput.trim(), page: 1 }))
  }

  return (
    <section className="card">
      <div className="card-header">
        <h2>Inventory · {organization.name}</h2>
        <Link to="/" state={{ organizationId }}>
          Dashboard
        </Link>
      </div>

      <ExpiringSoon
        organizationId={organizationId}
        timeZone={organization.time_zone}
        reloadKey={reloadCount}
        onChanged={reload}
      />

      {canManage &&
        (adding ? (
          <IngredientForm
            initial={{ name: '', unit: 'kg', reorderLevel: '' }}
            submitLabel="Add ingredient"
            onSubmit={async (ingredient) => {
              await createIngredient(organizationId, ingredient)
              setAdding(false)
              reload()
            }}
            onCancel={() => setAdding(false)}
          />
        ) : (
          <button type="button" onClick={() => setAdding(true)}>
            Add ingredient
          </button>
        ))}

      <form className="toolbar" onSubmit={handleSearch}>
        <input
          type="search"
          aria-label="Search ingredients"
          placeholder="Search ingredients"
          value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
        />
        <button type="submit">Search</button>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={query.archived}
            onChange={(event) =>
              setQuery((current) => ({ ...current, archived: event.target.checked, page: 1 }))
            }
          />
          Show archived
        </label>
      </form>

      {!result || result.query !== query ? (
        <p>Loading ingredients…</p>
      ) : result.results.length === 0 ? (
        <p className="muted">
          {query.search || query.archived
            ? 'No ingredients match.'
            : canManage
              ? 'No ingredients yet. Add the first one above.'
              : 'No ingredients yet. A manager or owner can add them.'}
        </p>
      ) : (
        <>
          <ul className="ingredient-list">
            {result.results.map((ingredient) => (
              <IngredientRow
                key={ingredient.id}
                organizationId={organizationId}
                timeZone={organization.time_zone}
                ingredient={ingredient}
                canManage={canManage}
                onChanged={replaceIngredient}
                onReload={reload}
              />
            ))}
          </ul>
          {(result.previous || result.next) && (
            <div className="actions">
              <button
                type="button"
                disabled={!result.previous}
                onClick={() => setQuery((current) => ({ ...current, page: current.page - 1 }))}
              >
                Previous
              </button>
              <span className="muted">Page {query.page}</span>
              <button
                type="button"
                disabled={!result.next}
                onClick={() => setQuery((current) => ({ ...current, page: current.page + 1 }))}
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </section>
  )
}
