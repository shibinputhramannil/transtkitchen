import { Navigate, Route, Routes, useLocation } from 'react-router'
import { useAuth } from './auth/useAuth.js'
import SystemStatus from './components/SystemStatus.jsx'
import CreateOrganizationPage from './pages/CreateOrganizationPage.jsx'
import DashboardPage from './pages/DashboardPage.jsx'
import LoginPage from './pages/LoginPage.jsx'
import RegisterPage from './pages/RegisterPage.jsx'
import './App.css'

// Pages for signed-in users; everyone else is sent to the sign-in page.
function RequireAuth({ children }) {
  const { user } = useAuth()
  const location = useLocation()
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return children
}

// Sign-in and registration; signed-in users go back where they were headed.
function SignedOutOnly({ children }) {
  const { user } = useAuth()
  const location = useLocation()
  if (user) {
    return <Navigate to={location.state?.from ?? '/'} replace />
  }
  return children
}

function App() {
  const { user, signOut } = useAuth()

  return (
    <main className="app">
      <header className="app-header">
        <div>
          <h1>KitchenMind AI</h1>
          <p className="tagline">
            Kitchen operations for independent restaurants, bakeries, and cloud kitchens.
          </p>
        </div>
        {user && (
          <div className="account">
            <span>{user.email}</span>
            <button type="button" onClick={signOut}>
              Sign out
            </button>
          </div>
        )}
      </header>

      <Routes>
        <Route path="/login" element={<SignedOutOnly><LoginPage /></SignedOutOnly>} />
        <Route path="/register" element={<SignedOutOnly><RegisterPage /></SignedOutOnly>} />
        <Route
          path="/organizations/new"
          element={<RequireAuth><CreateOrganizationPage /></RequireAuth>}
        />
        <Route path="/" element={<RequireAuth><DashboardPage /></RequireAuth>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>

      <SystemStatus />
    </main>
  )
}

export default App
