import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'
import {
  createPrepPlan,
  deletePrepPlan,
  errorMessages,
  getForecast,
  getOrganization,
  getPrepPlan,
  listPrepPlans,
  prepPlanAction,
  updatePrepPlan,
} from '../api/client.js'
import FormErrors from '../components/FormErrors.jsx'
import { formatQuantity } from '../format.js'
import { addDays, localDate } from '../timeZones.js'

// Plans can be made for today up to this many days ahead (the server checks too).
const MAX_DAYS_AHEAD = 14

function weekdayName(isoDate) {
  return new Date(`${isoDate}T00:00:00Z`).toLocaleDateString('en-US', { weekday: 'long', timeZone: 'UTC' })
}

function forecastText(line, planDate) {
  if (line.method === 'none') return 'Not enough sales history yet'
  const values = line.basis.map((day) => formatQuantity(day.quantity)).join(', ')
  return line.method === 'weekday_average'
    ? `Average of the last ${line.basis.length} ${weekdayName(planDate)}s: ${values}`
    : `Average of the last ${line.basis.length} trading days`
}

function when(timestamp) {
  return new Date(timestamp).toLocaleString()
}

function LiveIngredients({ current }) {
  return (
    <section className="figures live" aria-labelledby="live-heading">
      <h3 id="live-heading">Current stock (live)</h3>
      <p className="muted">
        As of {when(current.as_of)}. {current.note}
      </p>
      {current.items_without_recipe.length > 0 && (
        <p className="warning">
          No recipe, so their ingredient needs are unknown: {current.items_without_recipe.join(', ')}
        </p>
      )}
      {current.rows.length === 0 ? (
        <p className="muted">No ingredients needed for the planned portions.</p>
      ) : (
        <ul className="batch-list">
          {current.rows.map((row) => (
            <li key={row.ingredient.id}>
              <div className="ingredient-summary">
                <span className="ingredient-name">
                  {row.ingredient.name}
                  {!row.ingredient.is_active && <span className="badge">Archived</span>}
                </span>
                {Number(row.shortfall) > 0 ? (
                  <span className="badge badge-error">
                    Need {formatQuantity(row.shortfall)} {row.unit} more
                  </span>
                ) : (
                  <span className="badge badge-ok">Enough</span>
                )}
              </div>
              <span className="muted">
                Needs {formatQuantity(row.required)} {row.unit} · {formatQuantity(row.on_hand)} on hand now
                {Number(row.expiring_before) > 0 && (
                  <>, {formatQuantity(row.expiring_before)} of it expires before the plan date</>
                )}{' '}
                · {formatQuantity(row.usable)} usable
              </span>
              {row.use_first.length > 0 && (
                <span className="muted">
                  Use first:{' '}
                  {row.use_first.map((batch) => `${formatQuantity(batch.quantity)} ${row.unit} expiring ${batch.expires_on}`).join('; ')}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function SnapshotIngredients({ snapshot }) {
  return (
    <section className="figures snapshot" aria-labelledby="snapshot-heading">
      <h3 id="snapshot-heading">Saved when finalized: {when(snapshot.taken_at)}</h3>
      <p className="muted">{snapshot.note}</p>
      {snapshot.rows.length === 0 ? (
        <p className="muted">No ingredients were needed.</p>
      ) : (
        <ul className="batch-list">
          {snapshot.rows.map((row) => (
            <li key={row.ingredient.id} className="ingredient-summary">
              <span>
                {row.ingredient.name}{' '}
                <span className="muted">
                  · needed {formatQuantity(row.required)} {row.unit} · {formatQuantity(row.usable)} was usable then
                </span>
              </span>
              {Number(row.shortfall) > 0 && (
                <span className="badge">
                  was short {formatQuantity(row.shortfall)} {row.unit}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function PlanView({ organizationId, plan, today, canManage, onPlan, onDeleted }) {
  // Unsaved edits, kept across reloads so a conflict never loses typing.
  const [edits, setEdits] = useState({})
  const [buffer, setBuffer] = useState(null)
  const [errors, setErrors] = useState([])
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)

  const editable = canManage && plan.status === 'draft'
  const isPast = plan.plan_date < today
  const dirty = Object.keys(edits).length > 0 || (buffer !== null && buffer !== plan.buffer_percent)

  function edit(menuItemId, field, value) {
    setEdits((current) => ({ ...current, [menuItemId]: { ...current[menuItemId], [field]: value } }))
  }

  async function run(task) {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setErrors([])
    try {
      await task()
    } catch (error) {
      if (error.status === 409 && error.data?.version !== undefined) {
        // Someone else saved first. Show their version; unsaved edits stay in place.
        onPlan(await getPrepPlan(organizationId, plan.id))
        setErrors(['Someone else changed this plan, so it was reloaded. Your unsaved changes are still here; save again to apply them.'])
      } else {
        setErrors(errorMessages(error))
      }
    }
    inFlight.current = false
    setBusy(false)
  }

  const save = () =>
    run(async () => {
      const lines = Object.entries(edits).map(([menuItem, change]) => ({ menu_item: menuItem, ...change }))
      onPlan(
        await updatePrepPlan(organizationId, plan.id, {
          version: plan.version,
          bufferPercent: buffer !== null && buffer !== plan.buffer_percent ? buffer : undefined,
          lines,
        }),
      )
      setEdits({})
      setBuffer(null)
    })

  const act = (action) => run(async () => onPlan(await prepPlanAction(organizationId, plan.id, action, plan.version)))

  const remove = () =>
    run(async () => {
      await deletePrepPlan(organizationId, plan.id)
      onDeleted()
    })

  return (
    <>
      <div className="card-header">
        <h3>Plan for {weekdayName(plan.plan_date)} {plan.plan_date}</h3>
        <span className={`badge ${plan.status === 'final' ? 'badge-ok' : ''}`}>
          {plan.status === 'final' ? 'Final' : 'Draft'}
        </span>
      </div>
      {plan.status === 'final' && (
        <p className="muted">
          Finalized {when(plan.finalized_at)} by {plan.finalized_by ?? 'a former member'}.
        </p>
      )}
      {plan.review && !plan.review.sales_imported && (
        <p className="muted">Sales for this date have not been imported yet, so there is nothing to compare.</p>
      )}

      <label className="inline-label">
        Buffer on top of the forecast (%)
        <input
          type="number"
          min="0"
          max="200"
          step="0.01"
          disabled={!editable}
          value={buffer ?? plan.buffer_percent}
          onChange={(event) => setBuffer(event.target.value)}
        />
      </label>

      <ul className="ingredient-list">
        {plan.lines.map((line) => {
          const change = edits[line.menu_item.id] ?? {}
          return (
            <li key={line.menu_item.id}>
              <div className="ingredient-summary">
                <span className="ingredient-name">
                  {line.menu_item.name}
                  {!line.menu_item.is_active && <span className="badge">Archived</span>}
                  {line.method === 'none' && <span className="badge badge-warning">No forecast</span>}
                  {line.edited && <span className="badge">Edited</span>}
                </span>
                <span className="muted">
                  Forecast {line.forecast !== null ? formatQuantity(line.forecast) : '—'} · suggested{' '}
                  {formatQuantity(line.suggested)}
                </span>
              </div>
              <span className="muted">{forecastText(line, plan.plan_date)}</span>
              {line.recent_overproduction && (
                <span className="muted">
                  Overproduced {formatQuantity(line.recent_overproduction.portions)} portions on{' '}
                  {line.recent_overproduction.days} days in the last 4 weeks.
                </span>
              )}
              {line.review && (
                <span className="muted">
                  {line.review.sold !== null ? `Sold ${formatQuantity(line.review.sold)}` : 'Sales not imported'} ·
                  overproduced {formatQuantity(line.review.overproduced)}
                </span>
              )}
              {editable ? (
                <div className="form-row">
                  <label>
                    Planned portions
                    <input
                      type="number"
                      inputMode="decimal"
                      min="0"
                      step="0.001"
                      value={change.planned ?? line.planned}
                      onChange={(event) => edit(line.menu_item.id, 'planned', event.target.value)}
                    />
                  </label>
                  <label>
                    Note
                    <input
                      maxLength={255}
                      value={change.note ?? line.note}
                      onChange={(event) => edit(line.menu_item.id, 'note', event.target.value)}
                    />
                  </label>
                </div>
              ) : (
                <span>
                  <strong>Planned {formatQuantity(line.planned)}</strong>
                  {line.note && <span className="muted"> · {line.note}</span>}
                </span>
              )}
            </li>
          )
        })}
      </ul>

      <FormErrors errors={errors} />
      {canManage && (
        <div className="actions">
          {plan.status === 'draft' && (
            <>
              <button type="button" disabled={busy || !dirty} onClick={save}>
                Save changes
              </button>
              <button type="button" disabled={busy || dirty} onClick={() => act('refresh')}>
                Refresh forecast
              </button>
              <button type="button" disabled={busy || dirty} onClick={() => act('finalize')}>
                Finalize
              </button>
              <button type="button" disabled={busy} onClick={remove}>
                Delete draft
              </button>
            </>
          )}
          {plan.status === 'final' && !isPast && (
            <button type="button" disabled={busy} onClick={() => act('reopen')}>
              Reopen
            </button>
          )}
        </div>
      )}
      {dirty && <p className="muted">Save your changes before refreshing or finalizing.</p>}

      {plan.ingredient_snapshot && <SnapshotIngredients snapshot={plan.ingredient_snapshot} />}
      <LiveIngredients current={plan.current_ingredients} />
    </>
  )
}

export default function PrepPage() {
  const { organizationId } = useParams()
  const [organization, setOrganization] = useState(null)
  const [date, setDate] = useState(null)
  const [plans, setPlans] = useState(null)
  const [plan, setPlan] = useState(null)
  const [preview, setPreview] = useState(null)
  const [errors, setErrors] = useState([])
  const [creating, setCreating] = useState(false)
  const [reloadCount, setReloadCount] = useState(0)
  const reload = useCallback(() => setReloadCount((count) => count + 1), [])

  useEffect(() => {
    let cancelled = false
    getOrganization(organizationId).then(
      (org) => {
        if (!cancelled) {
          setOrganization(org)
          setDate(addDays(localDate(org.time_zone), 1))
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
    if (!organization || !date) return undefined
    let cancelled = false
    const today = localDate(organization.time_zone)
    listPrepPlans(organizationId, { from: addDays(today, -28), to: addDays(today, MAX_DAYS_AHEAD) }).then(
      async (data) => {
        const found = data.results.find((item) => item.plan_date === date)
        const detail = found ? await getPrepPlan(organizationId, found.id) : null
        const inWindow = date >= today && date <= addDays(today, MAX_DAYS_AHEAD)
        const forecast = !found && inWindow ? await getForecast(organizationId, date) : null
        if (!cancelled) {
          setPlans(data.results)
          setPlan(detail)
          setPreview(forecast)
        }
      },
      (error) => {
        if (!cancelled) setErrors(errorMessages(error))
      },
    )
    return () => {
      cancelled = true
    }
  }, [organizationId, organization, date, reloadCount])

  if (!organization || !date) {
    return (
      <section className="card">
        {errors.length ? <FormErrors errors={errors} /> : <p>Loading…</p>}
      </section>
    )
  }
  const canManage = organization.role === 'owner' || organization.role === 'manager'
  const today = localDate(organization.time_zone)
  const inWindow = date >= today && date <= addDays(today, MAX_DAYS_AHEAD)

  async function handleCreate() {
    if (creating) return
    setCreating(true)
    setErrors([])
    try {
      setPlan(await createPrepPlan(organizationId, date))
      reload()
    } catch (error) {
      // A plan made by someone else at the same moment: show it.
      if (error.status === 409 && error.data?.plan) reload()
      setErrors(errorMessages(error))
    }
    setCreating(false)
  }

  return (
    <section className="card">
      <div className="card-header">
        <h2>Prep · {organization.name}</h2>
        <Link to="/" state={{ organizationId }}>
          Dashboard
        </Link>
      </div>
      <p className="muted">
        Forecasts come from imported sales. Plans never change stock; sales and waste do.
      </p>
      <label className="inline-label">
        Date
        <input type="date" max={addDays(today, MAX_DAYS_AHEAD)} value={date} onChange={(event) => setDate(event.target.value)} />
      </label>
      <FormErrors errors={errors} />

      {plan ? (
        <PlanView
          key={plan.id}
          organizationId={organizationId}
          plan={plan}
          today={today}
          canManage={canManage}
          onPlan={setPlan}
          onDeleted={() => {
            setPlan(null)
            reload()
          }}
        />
      ) : !inWindow ? (
        <p className="muted">No plan was made for this date.</p>
      ) : (
        <>
          <h3>Forecast for {weekdayName(date)} {date}</h3>
          {preview && (
            <ul className="batch-list">
              {preview.results.map((row) => (
                <li key={row.menu_item.id} className="ingredient-summary">
                  <span>
                    {row.menu_item.name} <span className="muted">· {forecastText(row, date)}</span>
                  </span>
                  <strong>{row.forecast !== null ? formatQuantity(row.forecast) : '—'}</strong>
                </li>
              ))}
            </ul>
          )}
          {canManage ? (
            <button type="button" disabled={creating} onClick={handleCreate}>
              {creating ? 'Creating…' : 'Create plan'}
            </button>
          ) : (
            <p className="muted">No plan yet. Managers and owners create plans.</p>
          )}
        </>
      )}

      <h3>Recent and upcoming plans</h3>
      {plans && plans.length === 0 ? (
        <p className="muted">No plans yet.</p>
      ) : (
        <ul className="batch-list">
          {(plans ?? []).map((item) => (
            <li key={item.id} className="ingredient-summary">
              <button type="button" className="link-button" onClick={() => setDate(item.plan_date)}>
                {weekdayName(item.plan_date)} {item.plan_date}
              </button>
              <span className={`badge ${item.status === 'final' ? 'badge-ok' : ''}`}>
                {item.status === 'final' ? 'Final' : 'Draft'}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
