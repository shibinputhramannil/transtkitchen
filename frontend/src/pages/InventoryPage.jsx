import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  countStock,
  createIngredient,
  errorMessages,
  getOrganization,
  listIngredients,
  receiveStock,
  updateIngredient,
} from '../api/client.js'
import FormErrors from '../components/FormErrors.jsx'

const UNITS = ['g', 'kg', 'ml', 'l', 'each']

// "12.500" -> "12.5", "3.000" -> "3"; quantities stay strings, so no precision is lost.
function formatQuantity(quantity) {
  return quantity.includes('.') ? quantity.replace(/0+$/, '').replace(/\.$/, '') : quantity
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
function StockForm({ organizationId, ingredient, kind, onSaved, onConflict, onCancel }) {
  const [requestId] = useState(() => crypto.randomUUID())
  // A count is checked against the stock level the counter started from.
  const [expectedQuantity] = useState(ingredient.quantity_on_hand)
  const [quantity, setQuantity] = useState('')
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
        : await receiveStock(organizationId, ingredient.id, { quantity, note, requestId })
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

function IngredientRow({ organizationId, ingredient, canManage, onChanged, onReload }) {
  // null, 'receive', 'count', or 'edit'.
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
      {open === null && (
        <div className="actions">
          {ingredient.is_active && (
            <>
              <button type="button" onClick={() => setOpen('receive')}>
                Receive
              </button>
              <button type="button" onClick={() => setOpen('count')}>
                Count
              </button>
            </>
          )}
          {canManage && (
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
      <FormErrors errors={archiveErrors} />
      {(open === 'receive' || open === 'count') && (
        <StockForm
          organizationId={organizationId}
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
