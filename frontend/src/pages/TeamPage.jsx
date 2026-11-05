import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import {
  errorMessages,
  getOrganization,
  inviteMember,
  listMembers,
  listOrganizationInvitations,
  removeMembership,
  revokeInvitation,
  updateMembership,
} from '../api/client.js'
import { useAuth } from '../auth/useAuth.js'
import FormErrors from '../components/FormErrors.jsx'

const ROLE_LABELS = { owner: 'Owner', manager: 'Manager', staff: 'Staff' }

function displayName(user) {
  return [user.first_name, user.last_name].filter(Boolean).join(' ') || user.email
}

// What the interface offers; the server enforces the same rules and has the last word.
function canRemove(myRole, isSelf, targetRole) {
  return isSelf || myRole === 'owner' || (myRole === 'manager' && targetRole === 'staff')
}

function MemberRow({ organizationId, membership, myRole, isSelf, onChanged, onLeft }) {
  const [role, setRole] = useState(membership.role)
  const [confirming, setConfirming] = useState(false)
  const [errors, setErrors] = useState([])
  const [busy, setBusy] = useState(false)

  async function run(task) {
    if (busy) return
    setBusy(true)
    setErrors([])
    try {
      await task()
    } catch (error) {
      setErrors(errorMessages(error))
      setRole(membership.role)
    }
    setBusy(false)
  }

  const saveRole = () => run(async () => onChanged(await updateMembership(organizationId, membership.id, role)))

  const remove = () =>
    run(async () => {
      await removeMembership(organizationId, membership.id)
      setConfirming(false)
      if (isSelf) onLeft()
      else onChanged()
    })

  return (
    <li>
      <div className="ingredient-summary">
        <span>
          <strong>{displayName(membership.user)}</strong>
          {isSelf && <span className="muted"> (you)</span>}
          <span className="muted"> · {membership.user.email}</span>
        </span>
        <span className="badge">{ROLE_LABELS[membership.role]}</span>
      </div>
      <div className="actions">
        {myRole === 'owner' && (
          <>
            <select aria-label={`Role for ${membership.user.email}`} value={role} onChange={(event) => setRole(event.target.value)}>
              {Object.entries(ROLE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <button type="button" disabled={busy || role === membership.role} onClick={saveRole}>
              Change role
            </button>
          </>
        )}
        {canRemove(myRole, isSelf, membership.role) &&
          (confirming ? (
            <>
              <span className="muted">
                {isSelf ? 'Leave this organization?' : `Remove ${membership.user.email}?`}
              </span>
              <button type="button" className="button-danger" disabled={busy} onClick={remove}>
                {isSelf ? 'Yes, leave' : 'Yes, remove'}
              </button>
              <button type="button" disabled={busy} onClick={() => setConfirming(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button type="button" onClick={() => setConfirming(true)}>
              {isSelf ? 'Leave organization' : 'Remove'}
            </button>
          ))}
      </div>
      <FormErrors errors={errors} />
    </li>
  )
}

function InviteForm({ organizationId, myRole, onInvited }) {
  // Managers may only invite staff; owners may invite any role.
  const roles = myRole === 'owner' ? ['staff', 'manager', 'owner'] : ['staff']
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('staff')
  const [errors, setErrors] = useState([])
  const [sent, setSent] = useState(null)
  const [busy, setBusy] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setErrors([])
    setSent(null)
    try {
      const invitation = await inviteMember(organizationId, email, role)
      setSent(invitation.email)
      setEmail('')
      onInvited()
    } catch (error) {
      setErrors(errorMessages(error))
    }
    setBusy(false)
  }

  return (
    <form className="form inline-form" onSubmit={handleSubmit}>
      <p className="muted">
        Transt KitchenMind AI does not send emails yet. After inviting someone, tell them to sign up or sign in
        with exactly this email address; the invitation then appears on their dashboard. Invitations
        expire after 7 days.
      </p>
      <div className="form-row">
        <label>
          Email
          <input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} />
        </label>
        <label>
          Role
          <select value={role} onChange={(event) => setRole(event.target.value)}>
            {roles.map((value) => (
              <option key={value} value={value}>
                {ROLE_LABELS[value]}
              </option>
            ))}
          </select>
        </label>
      </div>
      <FormErrors errors={errors} />
      {sent && <p role="status">Invitation recorded for {sent}.</p>}
      <div className="actions">
        <button type="submit" disabled={busy}>
          {busy ? 'Inviting…' : 'Invite'}
        </button>
      </div>
    </form>
  )
}

function PendingInvitations({ organizationId, myRole, invitations, onChanged }) {
  const [errors, setErrors] = useState([])

  async function revoke(invitation) {
    setErrors([])
    try {
      await revokeInvitation(organizationId, invitation.id)
      onChanged()
    } catch (error) {
      setErrors(errorMessages(error))
    }
  }

  return (
    <>
      <h3>Pending invitations</h3>
      <FormErrors errors={errors} />
      {invitations.length === 0 ? (
        <p className="muted">No pending invitations.</p>
      ) : (
        <ul className="batch-list">
          {invitations.map((invitation) => (
            <li key={invitation.id} className="ingredient-summary">
              <span>
                <strong>{invitation.email}</strong>
                <span className="muted">
                  {' '}
                  · {ROLE_LABELS[invitation.role]} · invited by {invitation.invited_by ?? 'a former member'} ·
                  expires {new Date(invitation.expires_at).toLocaleDateString()}
                </span>
              </span>
              {(myRole === 'owner' || invitation.role === 'staff') && (
                <button type="button" onClick={() => revoke(invitation)}>
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

export default function TeamPage() {
  const { organizationId } = useParams()
  const { user } = useAuth()
  const navigate = useNavigate()
  const [organization, setOrganization] = useState(null)
  const [members, setMembers] = useState(null)
  const [invitations, setInvitations] = useState(null)
  const [errors, setErrors] = useState([])
  const [reloadCount, setReloadCount] = useState(0)
  const reload = useCallback(() => setReloadCount((count) => count + 1), [])

  useEffect(() => {
    let cancelled = false
    getOrganization(organizationId)
      .then(async (org) => {
        const canInvite = org.role === 'owner' || org.role === 'manager'
        const [memberList, invitationList] = await Promise.all([
          listMembers(organizationId),
          canInvite ? listOrganizationInvitations(organizationId) : Promise.resolve(null),
        ])
        if (!cancelled) {
          setOrganization(org)
          setMembers(memberList)
          setInvitations(invitationList?.results ?? null)
        }
      })
      .catch((error) => {
        if (!cancelled) setErrors(errorMessages(error))
      })
    return () => {
      cancelled = true
    }
  }, [organizationId, reloadCount])

  if (!organization || !members) {
    return (
      <section className="card">
        {errors.length ? <FormErrors errors={errors} /> : <p>Loading…</p>}
      </section>
    )
  }
  const myRole = organization.role
  const canInvite = myRole === 'owner' || myRole === 'manager'

  return (
    <section className="card">
      <div className="card-header">
        <h2>Team · {organization.name}</h2>
        <Link to="/" state={{ organizationId }}>
          Dashboard
        </Link>
      </div>
      <p className="muted">
        You are {myRole === 'owner' ? 'an owner' : `a ${myRole}`}.{' '}
        {myRole === 'owner'
          ? 'Owners change roles and remove anyone. An organization always keeps at least one owner.'
          : myRole === 'manager'
            ? 'Managers invite and remove staff.'
            : 'Staff can see the team and leave the organization.'}
      </p>

      <h3>Members ({members.count})</h3>
      <ul className="ingredient-list">
        {members.results.map((membership) => (
          <MemberRow
            key={`${membership.id}-${membership.role}`}
            organizationId={organizationId}
            membership={membership}
            myRole={myRole}
            isSelf={membership.user.id === user?.id}
            onChanged={reload}
            onLeft={() => navigate('/')}
          />
        ))}
      </ul>
      {members.next && <p className="muted">Showing the first {members.results.length} of {members.count} members.</p>}

      {canInvite && (
        <>
          <h3>Invite someone</h3>
          <InviteForm organizationId={organizationId} myRole={myRole} onInvited={reload} />
          <PendingInvitations
            organizationId={organizationId}
            myRole={myRole}
            invitations={invitations ?? []}
            onChanged={reload}
          />
        </>
      )}
    </section>
  )
}
