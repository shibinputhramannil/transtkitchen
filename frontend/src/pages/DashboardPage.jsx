import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router'
import {
  acceptInvitation,
  declineInvitation,
  errorMessages,
  listMembers,
  listMyInvitations,
  listOrganizations,
  updateOrganization,
} from '../api/client.js'
import FormErrors from '../components/FormErrors.jsx'
import { timeZoneOptions } from '../timeZones.js'

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

// Invitations addressed to the signed-in user's email. The server only lists
// and accepts invitations for that address.
function InvitationInbox({ onAccepted }) {
  const [invitations, setInvitations] = useState(null)
  const [errors, setErrors] = useState([])
  const [busy, setBusy] = useState(null)

  useEffect(() => {
    let cancelled = false
    listMyInvitations().then(
      (data) => {
        if (!cancelled) setInvitations(data.results)
      },
      (error) => {
        if (!cancelled) setErrors(errorMessages(error))
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

  async function respond(invitation, accept) {
    if (busy) return
    setBusy(invitation.id)
    setErrors([])
    try {
      const organization = accept ? await acceptInvitation(invitation.id) : await declineInvitation(invitation.id)
      setInvitations((current) => current.filter((item) => item.id !== invitation.id))
      if (accept) onAccepted(organization)
    } catch (error) {
      setErrors(errorMessages(error))
    }
    setBusy(null)
  }

  if (!invitations?.length && !errors.length) return null
  return (
    <section className="expiring" aria-labelledby="invitations-heading">
      <h3 id="invitations-heading">Invitations for you</h3>
      <FormErrors errors={errors} />
      <ul className="batch-list">
        {(invitations ?? []).map((invitation) => (
          <li key={invitation.id}>
            <div className="ingredient-summary">
              <span>
                <strong>{invitation.organization.name}</strong>
                <span className="muted">
                  {' '}
                  · as {ROLE_LABELS[invitation.role]} · from {invitation.invited_by ?? 'a former member'} · expires{' '}
                  {new Date(invitation.expires_at).toLocaleDateString()}
                </span>
              </span>
              <span className="actions">
                <button type="button" disabled={busy !== null} onClick={() => respond(invitation, true)}>
                  Accept
                </button>
                <button type="button" disabled={busy !== null} onClick={() => respond(invitation, false)}>
                  Decline
                </button>
              </span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

// Everyone sees the name; only owners can change it (the server enforces this too).
function NameSetting({ organization, onChanged }) {
  const [value, setValue] = useState(organization.name)
  const [errors, setErrors] = useState([])
  const [saving, setSaving] = useState(false)

  if (organization.role !== 'owner') return null

  async function handleSubmit(event) {
    event.preventDefault()
    if (saving) return
    setSaving(true)
    setErrors([])
    try {
      onChanged(await updateOrganization(organization.id, { name: value }))
    } catch (error) {
      setErrors(errorMessages(error))
    }
    setSaving(false)
  }

  return (
    <form className="toolbar" onSubmit={handleSubmit}>
      <label className="inline-label">
        Organization name
        <input required maxLength={150} value={value} onChange={(event) => setValue(event.target.value)} />
      </label>
      <button type="submit" disabled={saving || value.trim() === organization.name || !value.trim()}>
        {saving ? 'Saving…' : 'Rename'}
      </button>
      <FormErrors errors={errors} />
    </form>
  )
}

// Everyone sees the time zone; only owners can change it (the server enforces this too).
function TimeZoneSetting({ organization, onChanged }) {
  const [value, setValue] = useState(organization.time_zone)
  const [errors, setErrors] = useState([])
  const [saving, setSaving] = useState(false)

  if (organization.role !== 'owner') {
    return <p className="muted">Time zone: {organization.time_zone}</p>
  }

  async function handleSubmit(event) {
    event.preventDefault()
    if (saving) return
    setSaving(true)
    setErrors([])
    try {
      onChanged(await updateOrganization(organization.id, { time_zone: value }))
    } catch (error) {
      setErrors(errorMessages(error))
    }
    setSaving(false)
  }

  return (
    <form className="toolbar" onSubmit={handleSubmit}>
      <label className="inline-label">
        Time zone (expiry dates use this kitchen&apos;s local date)
        <select value={value} onChange={(event) => setValue(event.target.value)}>
          {timeZoneOptions(organization.time_zone).map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" disabled={saving || value === organization.time_zone}>
        {saving ? 'Saving…' : 'Save'}
      </button>
      <FormErrors errors={errors} />
    </form>
  )
}

export default function DashboardPage() {
  const location = useLocation()
  const [organizations, setOrganizations] = useState(null)
  const [errors, setErrors] = useState([])
  const [selectedId, setSelectedId] = useState(location.state?.organizationId ?? null)
  const [reloadCount, setReloadCount] = useState(0)

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
  }, [reloadCount])

  function handleAccepted(organization) {
    setSelectedId(organization.id)
    setReloadCount((count) => count + 1)
  }

  function replaceOrganization(updated) {
    setOrganizations((current) => current.map((candidate) => (candidate.id === updated.id ? updated : candidate)))
  }

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
        <InvitationInbox onAccepted={handleAccepted} />
        <p>
          Create an organization for your restaurant, bakery, or cloud kitchen to get started, or accept
          an invitation from your team.
        </p>
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
      <InvitationInbox onAccepted={handleAccepted} />
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
      <NameSetting key={`name-${organization.id}`} organization={organization} onChanged={replaceOrganization} />
      <TimeZoneSetting key={organization.id} organization={organization} onChanged={replaceOrganization} />
      <div className="actions">
        <Link className="button" to={`/organizations/${organization.id}/inventory`}>
          Inventory
        </Link>
        <Link className="button" to={`/organizations/${organization.id}/menu`}>
          Menu
        </Link>
        <Link className="button" to={`/organizations/${organization.id}/sales`}>
          Sales
        </Link>
        <Link className="button" to={`/organizations/${organization.id}/waste`}>
          Waste
        </Link>
        <Link className="button" to={`/organizations/${organization.id}/prep`}>
          Prep
        </Link>
        <Link className="button" to={`/organizations/${organization.id}/team`}>
          Team
        </Link>
      </div>
      <MemberList organizationId={organization.id} />
      <p>
        <Link to="/organizations/new">Create another organization</Link>
      </p>
    </section>
  )
}
