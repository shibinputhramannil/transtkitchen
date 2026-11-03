import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import {
  errorMessages,
  getDailySales,
  getOrganization,
  getSalesTemplate,
  listSalesImports,
  uploadSalesImport,
} from '../api/client.js'
import FormErrors from '../components/FormErrors.jsx'
import { formatQuantity } from '../format.js'

const STATUS_LABELS = { draft: 'Draft', applied: 'Applied', discarded: 'Discarded' }

function UploadForm({ organizationId }) {
  const navigate = useNavigate()
  const fileInput = useRef(null)
  const [errors, setErrors] = useState([])
  const [uploading, setUploading] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    const file = fileInput.current?.files?.[0]
    if (!file || uploading) return
    setUploading(true)
    setErrors([])
    try {
      // Uploading the same file again returns the same draft, so retrying is safe.
      const draft = await uploadSalesImport(organizationId, file)
      navigate(`/organizations/${organizationId}/sales/imports/${draft.id}`)
    } catch (error) {
      setErrors(errorMessages(error))
      setUploading(false)
    }
  }

  async function handleTemplate() {
    try {
      const text = await getSalesTemplate(organizationId)
      const url = URL.createObjectURL(new Blob([text], { type: 'text/csv' }))
      const link = document.createElement('a')
      link.href = url
      link.download = 'sales-template.csv'
      link.click()
      URL.revokeObjectURL(url)
    } catch (error) {
      setErrors(errorMessages(error))
    }
  }

  return (
    <form className="form inline-form" onSubmit={handleSubmit}>
      <label>
        Sales CSV file
        <input ref={fileInput} type="file" accept=".csv,text/csv" required />
      </label>
      <p className="hint">
        Columns: date (YYYY-MM-DD or an ISO date-time), item, quantity, and optionally item_id. Up to
        5,000 rows and 2 MB. You will see a preview before anything changes.
      </p>
      <FormErrors errors={errors} />
      <div className="actions">
        <button type="submit" disabled={uploading}>
          {uploading ? 'Uploading…' : 'Upload and preview'}
        </button>
        <button type="button" onClick={handleTemplate}>
          Download template
        </button>
      </div>
    </form>
  )
}

function DailySales({ organizationId }) {
  const [report, setReport] = useState(null)
  const [errors, setErrors] = useState([])

  useEffect(() => {
    let cancelled = false
    getDailySales(organizationId).then(
      (data) => {
        if (!cancelled) setReport(data)
      },
      (error) => {
        if (!cancelled) setErrors(errorMessages(error))
      },
    )
    return () => {
      cancelled = true
    }
  }, [organizationId])

  return (
    <section aria-labelledby="daily-heading">
      <h3 id="daily-heading">Sales by business day</h3>
      <FormErrors errors={errors} />
      {report && (
        <>
          <p className="muted">
            {report.from} to {report.to}, from applied imports, in the kitchen&apos;s time zone.
          </p>
          {report.results.length === 0 ? (
            <p className="muted">No applied sales in this period.</p>
          ) : (
            <ul className="batch-list">
              {report.results.map((row) => (
                <li key={`${row.business_date}-${row.menu_item.id}`} className="ingredient-summary">
                  <span>
                    <span className="muted">{row.business_date}</span> · {row.menu_item.name}
                  </span>
                  <strong>{formatQuantity(row.quantity)}</strong>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  )
}

export default function SalesPage() {
  const { organizationId } = useParams()
  const [organization, setOrganization] = useState(null)
  const [imports, setImports] = useState(null)
  const [errors, setErrors] = useState([])

  useEffect(() => {
    let cancelled = false
    Promise.all([getOrganization(organizationId), listSalesImports(organizationId)]).then(
      ([org, data]) => {
        if (!cancelled) {
          setOrganization(org)
          setImports(data)
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

  if (!organization) {
    return (
      <section className="card">
        {errors.length ? <FormErrors errors={errors} /> : <p>Loading…</p>}
      </section>
    )
  }
  const canImport = organization.role === 'owner' || organization.role === 'manager'

  return (
    <section className="card">
      <div className="card-header">
        <h2>Sales · {organization.name}</h2>
        <Link to="/" state={{ organizationId }}>
          Dashboard
        </Link>
      </div>
      {canImport ? (
        <UploadForm organizationId={organizationId} />
      ) : (
        <p className="muted">Managers and owners import sales.</p>
      )}

      <h3>Imports</h3>
      {imports.results.length === 0 ? (
        <p className="muted">No imports yet.</p>
      ) : (
        <ul className="batch-list">
          {imports.results.map((item) => (
            <li key={item.id} className="ingredient-summary">
              <span>
                <Link to={`/organizations/${organizationId}/sales/imports/${item.id}`}>
                  {item.file_name || 'Import'}
                </Link>
                <span className="muted">
                  {' '}
                  · {item.first_date ?? '?'} to {item.last_date ?? '?'} · {item.row_count} rows
                </span>
              </span>
              <span className={`badge ${item.status === 'applied' ? 'badge-ok' : ''}`}>
                {STATUS_LABELS[item.status]}
              </span>
            </li>
          ))}
        </ul>
      )}

      <DailySales organizationId={organizationId} />
    </section>
  )
}
