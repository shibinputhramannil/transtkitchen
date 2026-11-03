import { useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { createOrganization, errorMessages } from '../api/client.js'
import FormErrors from '../components/FormErrors.jsx'
import { browserTimeZone } from '../timeZones.js'

export default function CreateOrganizationPage() {
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [errors, setErrors] = useState([])
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    setSubmitting(true)
    setErrors([])
    try {
      let organization
      try {
        // Expiry dates follow the kitchen's local date; start from this browser's time zone.
        organization = await createOrganization(name, browserTimeZone())
      } catch (error) {
        if (!error.data?.time_zone) throw error
        // The server does not know this browser's zone name; start in UTC instead.
        // The owner can change it on the dashboard.
        organization = await createOrganization(name)
      }
      navigate('/', { state: { organizationId: organization.id } })
    } catch (error) {
      setErrors(errorMessages(error))
      setSubmitting(false)
    }
  }

  return (
    <section className="card">
      <h2>Create an organization</h2>
      <p className="muted">
        Your restaurant, bakery, or cloud kitchen. You will be its owner and can invite your team
        later.
      </p>
      <form className="form" onSubmit={handleSubmit}>
        <label>
          Name
          <input
            required
            maxLength={150}
            autoComplete="organization"
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <FormErrors errors={errors} />
        <button type="submit" disabled={submitting}>
          {submitting ? 'Creating…' : 'Create organization'}
        </button>
      </form>
      <p>
        <Link to="/">Back to dashboard</Link>
      </p>
    </section>
  )
}
