import { Link, Navigate, Route, Routes, useLocation } from 'react-router'
import { useAuth } from './auth/useAuth.js'
import SystemStatus from './components/SystemStatus.jsx'
import AccountPage from './pages/AccountPage.jsx'
import CreateOrganizationPage from './pages/CreateOrganizationPage.jsx'
import DashboardPage from './pages/DashboardPage.jsx'
import InventoryPage from './pages/InventoryPage.jsx'
import MenuPage from './pages/MenuPage.jsx'
import SalesImportPage from './pages/SalesImportPage.jsx'
import SalesPage from './pages/SalesPage.jsx'
import WastePage from './pages/WastePage.jsx'
import PrepPage from './pages/PrepPage.jsx'
import TeamPage from './pages/TeamPage.jsx'
import LoginPage from './pages/LoginPage.jsx'
import RegisterPage from './pages/RegisterPage.jsx'
import transtLogo from './assets/brand/transt-logo.png'
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
          <h1 className="brand-title">
            <img className="brand-logo" src={transtLogo} alt="Transt" width="355" height="120" />{' '}
            <span className="brand-product">KitchenMind AI</span>
          </h1>
          <p className="tagline">
            Kitchen operations for independent restaurants, bakeries, and cloud kitchens.
          </p>
        </div>
        {user && (
          <div className="account">
            <span>{user.email}</span>
            <Link to="/account">Account</Link>
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
        <Route
          path="/organizations/:organizationId/inventory"
          element={<RequireAuth><InventoryPage /></RequireAuth>}
        />
        <Route
          path="/organizations/:organizationId/menu"
          element={<RequireAuth><MenuPage /></RequireAuth>}
        />
        <Route
          path="/organizations/:organizationId/sales"
          element={<RequireAuth><SalesPage /></RequireAuth>}
        />
        <Route
          path="/organizations/:organizationId/sales/imports/:importId"
          element={<RequireAuth><SalesImportPage /></RequireAuth>}
        />
        <Route
          path="/organizations/:organizationId/waste"
          element={<RequireAuth><WastePage /></RequireAuth>}
        />
        <Route
          path="/organizations/:organizationId/prep"
          element={<RequireAuth><PrepPage /></RequireAuth>}
        />
        <Route
          path="/organizations/:organizationId/team"
          element={<RequireAuth><TeamPage /></RequireAuth>}
        />
        <Route path="/account" element={<RequireAuth><AccountPage /></RequireAuth>} />
        <Route path="/" element={<RequireAuth><DashboardPage /></RequireAuth>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>

      <SystemStatus />
    </main>
  )
}

export default App
