import { useState } from 'react'
import { Link } from 'react-router'
import { errorMessages } from '../api/client.js'
import { useAuth } from '../auth/useAuth.js'
import FormErrors from '../components/FormErrors.jsx'

function NameForm() {
  const { user, updateProfile } = useAuth()
  const [firstName, setFirstName] = useState(user.first_name)
  const [lastName, setLastName] = useState(user.last_name)
  const [errors, setErrors] = useState([])
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setErrors([])
    setSaved(false)
    try {
      await updateProfile({ first_name: firstName, last_name: lastName })
      setSaved(true)
    } catch (error) {
      setErrors(errorMessages(error))
    }
    setBusy(false)
  }

  return (
    <form className="form" onSubmit={handleSubmit}>
      <h3>Name</h3>
      <div className="form-row">
        <label>
          First name
          <input autoComplete="given-name" maxLength={150} value={firstName} onChange={(event) => setFirstName(event.target.value)} />
        </label>
        <label>
          Last name
          <input autoComplete="family-name" maxLength={150} value={lastName} onChange={(event) => setLastName(event.target.value)} />
        </label>
      </div>
      <FormErrors errors={errors} />
      {saved && <p role="status">Name saved.</p>}
      <button type="submit" disabled={busy}>
        {busy ? 'Saving…' : 'Save name'}
      </button>
    </form>
  )
}

function PasswordForm() {
  const { changePassword } = useAuth()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [errors, setErrors] = useState([])
  const [changed, setChanged] = useState(false)
  const [busy, setBusy] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    if (busy) return
    setErrors([])
    setChanged(false)
    if (next !== confirm) {
      setErrors(['The new passwords do not match.'])
      return
    }
    setBusy(true)
    try {
      await changePassword(current, next)
      setCurrent('')
      setNext('')
      setConfirm('')
      setChanged(true)
    } catch (error) {
      setErrors(errorMessages(error))
    }
    setBusy(false)
  }

  return (
    <form className="form" onSubmit={handleSubmit}>
      <h3>Password</h3>
      <p className="muted">
        Changing your password signs you out everywhere else, including shared kitchen devices. This
        tab stays signed in.
      </p>
      <label>
        Current password
        <input type="password" autoComplete="current-password" required value={current} onChange={(event) => setCurrent(event.target.value)} />
      </label>
      <label>
        New password
        <input type="password" autoComplete="new-password" required minLength={8} value={next} onChange={(event) => setNext(event.target.value)} />
      </label>
      <label>
        New password again
        <input type="password" autoComplete="new-password" required minLength={8} value={confirm} onChange={(event) => setConfirm(event.target.value)} />
      </label>
      <FormErrors errors={errors} />
      {changed && <p role="status">Password changed. Other devices have been signed out.</p>}
      <button type="submit" disabled={busy}>
        {busy ? 'Changing…' : 'Change password'}
      </button>
    </form>
  )
}

export default function AccountPage() {
  const { user } = useAuth()

  return (
    <section className="card">
      <div className="card-header">
        <h2>Your account</h2>
        <Link to="/">Dashboard</Link>
      </div>
      <p className="muted">Signed in as {user.email}. Your email address cannot be changed here.</p>
      <NameForm />
      <PasswordForm />
    </section>
  )
}
