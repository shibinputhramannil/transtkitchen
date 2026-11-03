import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  createMenuItem,
  errorMessages,
  getOrganization,
  listAllIngredients,
  listMenuItems,
  saveRecipe,
  updateMenuItem,
} from '../api/client.js'
import FormErrors from '../components/FormErrors.jsx'
import { compatibleUnits, formatQuantity } from '../format.js'

function recipeSummary(recipe) {
  if (!recipe.length) return 'No recipe: sales of this item deduct no stock.'
  return recipe
    .map((line) => `${formatQuantity(line.quantity)} ${line.unit} ${line.ingredient.name}`)
    .join(' · ')
}

function RecipeEditor({ organizationId, item, ingredients, onSaved, onCancel }) {
  const [lines, setLines] = useState(() =>
    item.recipe.map((line) => ({
      ingredient: line.ingredient.id,
      quantity: line.quantity,
      unit: line.unit,
    })),
  )
  const [errors, setErrors] = useState([])
  const [saving, setSaving] = useState(false)
  const unitOf = (id) => ingredients.find((ingredient) => ingredient.id === id)?.unit

  function update(index, changes) {
    setLines((current) => current.map((line, i) => (i === index ? { ...line, ...changes } : line)))
  }

  function chooseIngredient(index, id) {
    // Default to the ingredient's own unit; keep the current one if compatible.
    const unit = unitOf(id)
    const line = lines[index]
    update(index, { ingredient: id, unit: compatibleUnits(unit).includes(line.unit) ? line.unit : unit })
  }

  async function handleSubmit(event) {
    event.preventDefault()
    if (saving) return
    setSaving(true)
    setErrors([])
    try {
      onSaved(await saveRecipe(organizationId, item.id, lines))
    } catch (error) {
      const lineErrors = error.data?.lines
      setErrors(
        Array.isArray(lineErrors)
          ? lineErrors.flatMap((problem, index) =>
              Object.values(problem ?? {})
                .flat()
                .map((message) => `Line ${index + 1}: ${message}`),
            )
          : errorMessages(error),
      )
      setSaving(false)
    }
  }

  return (
    <form className="form inline-form" onSubmit={handleSubmit}>
      <p className="muted">Quantities are per portion sold.</p>
      {lines.map((line, index) => (
        <div className="form-row" key={index}>
          <label>
            Ingredient
            <select required value={line.ingredient} onChange={(event) => chooseIngredient(index, event.target.value)}>
              <option value="">Choose…</option>
              {ingredients.map((ingredient) => (
                <option key={ingredient.id} value={ingredient.id}>
                  {ingredient.name} ({ingredient.unit})
                </option>
              ))}
            </select>
          </label>
          <label>
            Quantity
            <input
              type="number"
              inputMode="decimal"
              required
              min="0.001"
              step="0.001"
              value={line.quantity}
              onChange={(event) => update(index, { quantity: event.target.value })}
            />
          </label>
          <label>
            Unit
            <select
              value={line.unit}
              disabled={!line.ingredient}
              onChange={(event) => update(index, { unit: event.target.value })}
            >
              {compatibleUnits(unitOf(line.ingredient) ?? line.unit).map((unit) => (
                <option key={unit} value={unit}>
                  {unit}
                </option>
              ))}
            </select>
          </label>
          <div className="actions align-end">
            <button type="button" onClick={() => setLines((current) => current.filter((_, i) => i !== index))}>
              Remove
            </button>
          </div>
        </div>
      ))}
      <div className="actions">
        <button
          type="button"
          onClick={() => setLines((current) => [...current, { ingredient: '', quantity: '', unit: 'kg' }])}
        >
          Add ingredient
        </button>
      </div>
      <FormErrors errors={errors} />
      <div className="actions">
        <button type="submit" disabled={saving}>
          {saving ? 'Saving…' : 'Save recipe'}
        </button>
        <button type="button" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
      </div>
    </form>
  )
}

function MenuItemRow({ organizationId, item, ingredients, canManage, onChanged }) {
  // null, 'recipe', or 'rename'.
  const [open, setOpen] = useState(null)
  const [name, setName] = useState(item.name)
  const [errors, setErrors] = useState([])
  const [busy, setBusy] = useState(false)

  async function change(changes) {
    if (busy) return
    setBusy(true)
    setErrors([])
    try {
      onChanged(await updateMenuItem(organizationId, item.id, changes))
      setOpen(null)
    } catch (error) {
      setErrors(errorMessages(error))
    }
    setBusy(false)
  }

  return (
    <li>
      <div className="ingredient-summary">
        <span className="ingredient-name">
          {item.name}
          {!item.is_active && <span className="badge">Archived</span>}
        </span>
      </div>
      <span className="muted">{recipeSummary(item.recipe)}</span>
      {canManage && open === null && (
        <div className="actions">
          <button type="button" onClick={() => setOpen('recipe')}>
            Edit recipe
          </button>
          <button type="button" onClick={() => setOpen('rename')}>
            Rename
          </button>
          <button type="button" disabled={busy} onClick={() => change({ is_active: !item.is_active })}>
            {item.is_active ? 'Archive' : 'Restore'}
          </button>
        </div>
      )}
      {open === 'rename' && (
        <form
          className="toolbar"
          onSubmit={(event) => {
            event.preventDefault()
            change({ name })
          }}
        >
          <input aria-label="Menu item name" required maxLength={150} value={name} onChange={(event) => setName(event.target.value)} />
          <button type="submit" disabled={busy}>
            Save
          </button>
          <button type="button" onClick={() => setOpen(null)}>
            Cancel
          </button>
        </form>
      )}
      {open === 'recipe' && (
        <RecipeEditor
          organizationId={organizationId}
          item={item}
          ingredients={ingredients}
          onSaved={(updated) => {
            onChanged(updated)
            setOpen(null)
          }}
          onCancel={() => setOpen(null)}
        />
      )}
      <FormErrors errors={errors} />
    </li>
  )
}

export default function MenuPage() {
  const { organizationId } = useParams()
  const [organization, setOrganization] = useState(null)
  const [ingredients, setIngredients] = useState([])
  const [archived, setArchived] = useState(false)
  const [result, setResult] = useState(null)
  const [errors, setErrors] = useState([])
  const [newName, setNewName] = useState('')
  const [adding, setAdding] = useState(false)
  const [reloadCount, setReloadCount] = useState(0)

  useEffect(() => {
    let cancelled = false
    Promise.all([getOrganization(organizationId), listAllIngredients(organizationId)]).then(
      ([org, all]) => {
        if (!cancelled) {
          setOrganization(org)
          setIngredients(all)
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
    listMenuItems(organizationId, { archived }).then(
      (data) => {
        if (!cancelled) setResult({ archived, ...data })
      },
      (error) => {
        if (!cancelled) setErrors(errorMessages(error))
      },
    )
    return () => {
      cancelled = true
    }
  }, [organizationId, archived, reloadCount])

  if (!organization) {
    return (
      <section className="card">
        {errors.length ? <FormErrors errors={errors} /> : <p>Loading…</p>}
      </section>
    )
  }
  const canManage = organization.role === 'owner' || organization.role === 'manager'

  async function handleAdd(event) {
    event.preventDefault()
    if (adding) return
    setAdding(true)
    setErrors([])
    try {
      await createMenuItem(organizationId, newName)
      setNewName('')
      setReloadCount((count) => count + 1)
    } catch (error) {
      setErrors(errorMessages(error))
    }
    setAdding(false)
  }

  return (
    <section className="card">
      <div className="card-header">
        <h2>Menu · {organization.name}</h2>
        <Link to="/" state={{ organizationId }}>
          Dashboard
        </Link>
      </div>
      {canManage && (
        <form className="toolbar" onSubmit={handleAdd}>
          <input
            aria-label="New menu item name"
            placeholder="New menu item"
            required
            maxLength={150}
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
          />
          <button type="submit" disabled={adding}>
            Add menu item
          </button>
        </form>
      )}
      <FormErrors errors={errors} />
      <label className="checkbox">
        <input type="checkbox" checked={archived} onChange={(event) => setArchived(event.target.checked)} />
        Show archived
      </label>
      {!result || result.archived !== archived ? (
        <p>Loading menu…</p>
      ) : result.results.length === 0 ? (
        <p className="muted">{archived ? 'No archived menu items.' : 'No menu items yet.'}</p>
      ) : (
        <ul className="ingredient-list">
          {result.results.map((item) => (
            <MenuItemRow
              key={item.id}
              organizationId={organizationId}
              item={item}
              ingredients={ingredients}
              canManage={canManage}
              onChanged={() => setReloadCount((count) => count + 1)}
            />
          ))}
        </ul>
      )}
      {result?.next && <p className="muted">Showing the first {result.results.length} of {result.count}.</p>}
    </section>
  )
}
