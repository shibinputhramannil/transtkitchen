export default function FormErrors({ errors }) {
  if (!errors.length) {
    return null
  }
  return (
    <ul className="form-errors" role="alert">
      {errors.map((message) => (
        <li key={message}>{message}</li>
      ))}
    </ul>
  )
}
