import { useState } from 'react'
import { Link } from 'react-router'
import { errorMessages } from '../api/client.js'
import { useAuth } from '../auth/useAuth.js'
import FormErrors from '../components/FormErrors.jsx'

const EMPTY = { firstName: '', lastName: '', email: '', password: '' }

export default function RegisterPage() {
  const { signUp } = useAuth()
  const [account, setAccount] = useState(EMPTY)
  const [errors, setErrors] = useState([])
  const [submitting, setSubmitting] = useState(false)

  function update(field) {
    return (event) => setAccount((current) => ({ ...current, [field]: event.target.value }))
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setSubmitting(true)
    setErrors([])
    try {
      // On success the user is signed in and the router leaves this page.
      await signUp(account)
    } catch (error) {
      setErrors(errorMessages(error))
      setSubmitting(false)
    }
  }

  return (
    <section className="card">
      <h2>Create an account</h2>
      <form className="form" onSubmit={handleSubmit}>
        <div className="form-row">
          <label>
            First name
            <input autoComplete="given-name" value={account.firstName} onChange={update('firstName')} />
          </label>
          <label>
            Last name
            <input autoComplete="family-name" value={account.lastName} onChange={update('lastName')} />
          </label>
        </div>
        <label>
          Email
          <input
            type="email"
            autoComplete="email"
            required
            value={account.email}
            onChange={update('email')}
          />
        </label>
        <div className="field">
          <label>
            Password
            <input
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              aria-describedby="password-hint"
              value={account.password}
              onChange={update('password')}
            />
          </label>
          <span id="password-hint" className="hint">
            At least 8 characters, not entirely numbers, and not a common password.
          </span>
        </div>
        <FormErrors errors={errors} />
        <button type="submit" disabled={submitting}>
          {submitting ? 'Creating account…' : 'Create account'}
        </button>
      </form>
      <p>
        Already have an account? <Link to="/login">Sign in</Link>
      </p>
    </section>
  )
}
