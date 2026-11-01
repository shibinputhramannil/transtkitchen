import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router'
import { errorMessages, listMembers, listOrganizations } from '../api/client.js'
import FormErrors from '../components/FormErrors.jsx'

const ROLE_LABELS = { owner: 'Owner', manager: 'Manager', staff: 'Staff' }

function displayName(user) {
  return [user.first_name, user.last_name].filter(Boolean).join(' ') || user.email
}

function MemberList({ organizationId }) {
  const [result, setResult] = useState({ organizationId: null, members: null, errors: [] })

  useEffect(() => {
    let cancelled = false
    listMembers(organizationId).then(
      (data) => {
        if (!cancelled) setResult({ organizationId, members: data, errors: [] })
      },
      (error) => {
        if (!cancelled) setResult({ organizationId, members: null, errors: errorMessages(error) })
      },
    )
    return () => {
      cancelled = true
    }
  }, [organizationId])

  // Results for a previously selected organization count as still loading.
  if (result.organizationId !== organizationId) {
    return <p>Loading members…</p>
  }
  if (!result.members) {
    return <FormErrors errors={result.errors} />
  }

  const { count, results } = result.members
  return (
    <>
      <h3>Members ({count})</h3>
      <ul className="member-list">
        {results.map((membership) => (
          <li key={membership.id}>
            <span>
              {displayName(membership.user)}
              <span className="muted"> · {membership.user.email}</span>
            </span>
            <span className="badge">{ROLE_LABELS[membership.role]}</span>
          </li>
        ))}
      </ul>
      {count > results.length && (
        <p className="muted">
          Showing the first {results.length} of {count} members.
        </p>
      )}
    </>
  )
}

export default function DashboardPage() {
  const location = useLocation()
  const [organizations, setOrganizations] = useState(null)
  const [errors, setErrors] = useState([])
  const [selectedId, setSelectedId] = useState(location.state?.organizationId ?? null)

  useEffect(() => {
    let cancelled = false
    listOrganizations().then(
      (data) => {
        if (!cancelled) setOrganizations(data.results)
      },
      (error) => {
        if (!cancelled) setErrors(errorMessages(error))
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

  if (errors.length) {
    return (
      <section className="card">
        <FormErrors errors={errors} />
      </section>
    )
  }
  if (!organizations) {
    return (
      <section className="card">
        <p>Loading…</p>
      </section>
    )
  }
  if (!organizations.length) {
    return (
      <section className="card">
        <h2>Welcome to KitchenMind</h2>
        <p>Create an organization for your restaurant, bakery, or cloud kitchen to get started.</p>
        <Link className="button" to="/organizations/new">
          Create an organization
        </Link>
      </section>
    )
  }

  const organization =
    organizations.find((candidate) => candidate.id === selectedId) ?? organizations[0]

  return (
    <section className="card">
      <div className="card-header">
        <h2>{organization.name}</h2>
        <span className="badge">{ROLE_LABELS[organization.role]}</span>
      </div>
      {organizations.length > 1 && (
        <label className="inline-label">
          Organization
          <select value={organization.id} onChange={(event) => setSelectedId(event.target.value)}>
            {organizations.map((candidate) => (
              <option key={candidate.id} value={candidate.id}>
                {candidate.name}
              </option>
            ))}
          </select>
        </label>
      )}
      <MemberList organizationId={organization.id} />
      <p>
        <Link to="/organizations/new">Create another organization</Link>
      </p>
    </section>
  )
}
