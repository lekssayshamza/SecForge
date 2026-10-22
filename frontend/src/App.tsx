import { hasCreatorAccess } from './lib/permissions'
import { Routes, Route, Navigate, Outlet, useLocation } from 'react-router-dom'
import { useEffect } from 'react'
import { useAuth } from './context/AuthContext'
import Login from './pages/Login'
import Register from './pages/Register'
import Dashboard from './pages/Dashboard'
import LearnerDashboard from './pages/LearnerDashboard'
import Leaderboard from './pages/Leaderboard'
import RoomDetail from './pages/RoomDetail'
import LearningPaths, { LearningPathDetail } from './pages/LearningPaths'
import AppLayout from './components/AppLayout'
import SiteHeader from './components/SiteHeader'
import AccountSettings from './pages/AccountSettings'
import Landing from './pages/Landing'
import CanonicalProfile, { ProfileAlias } from './pages/CanonicalProfile'
import Saved from './pages/Saved'
import CreatorAnalytics from './pages/CreatorAnalytics'
import Administration from './pages/Administration'
import PasswordChange from './pages/PasswordChange'
import GoogleComplete from './pages/GoogleComplete'
import { PublicCatalog, PublicRoomDetail } from './pages/PublicRooms'
import './App.css'

function PrivateRoute() {
  const { user, isLoading } = useAuth()
  const location = useLocation()

  if (isLoading) {
    return <div className="loading" role="status"><span className="spinner" aria-hidden="true" /> Opening SecForge…</div>
  }

  if (user?.must_change_password && location.pathname !== '/password') return <Navigate to="/password" replace />
  return user ? <Outlet /> : <Navigate to="/login" state={{ from: location }} replace />
}

function PublicOnlyRoute({ children }: { children: React.ReactNode }) {
  const { user, isLoading } = useAuth()

  if (isLoading) {
    return <div className="loading" role="status"><span className="spinner" aria-hidden="true" /> Opening SecForge…</div>
  }

  return user ? <Navigate to="/" replace /> : <>{children}</>
}

function HomeRoute() {
  const { user, isLoading } = useAuth()
  if (isLoading) return <div className="loading" role="status"><span className="spinner" aria-hidden="true" /> Opening SecForge…</div>
  if (user?.must_change_password) return <Navigate to="/password" replace />
  return user ? <Outlet /> : <Landing />
}

function BrowseRoute({ detail = false }: { detail?: boolean }) {
  const { user, isLoading } = useAuth()
  const location = useLocation()
  if (isLoading) return <div className="loading" role="status"><span className="spinner" aria-hidden="true" /> Opening SecForge…</div>
  if (user?.must_change_password) return <Navigate to="/password" replace />
  return user ? (detail ? <RoomDetail key={location.pathname} /> : <PublicCatalog />) : (detail ? <PublicRoomDetail /> : <PublicCatalog />)
}

function OverviewRoute() {
  return <LearnerDashboard />
}

function CreatorWorkspaceRoute() {
  const { user } = useAuth()
  return hasCreatorAccess(user) ? <Dashboard catalogOnly /> : <Navigate to="/" replace />
}

function App() {
  const location = useLocation()
  const { user } = useAuth()
  useEffect(() => { window.scrollTo({ top: 0, left: 0, behavior: 'instant' }) }, [location.pathname])
  useEffect(() => {
    const title = location.pathname === '/login' ? 'Sign in' : location.pathname === '/register'
      ? 'Create account' : location.pathname.startsWith('/paths') ? 'Learning paths' : location.pathname === '/leaderboard' ? 'Leaderboard' : location.pathname.startsWith('/learners/') ? 'Learner profile' : location.pathname === '/settings' ? 'Account settings' : location.pathname === '/profile' ? 'Your profile' : location.pathname === '/' ? (user ? 'Overview' : 'Hands-on cybersecurity learning') : 'Rooms'
    document.title = `SecForge · ${title}`
  }, [location.pathname, user])
  return (
    <div className="app">
      <SiteHeader />
      <Routes>
        <Route path="/auth/google" element={<GoogleComplete />} />
        <Route element={<PrivateRoute />}><Route path="/password" element={<PasswordChange />} /></Route>
        <Route
          path="/login"
          element={
            <PublicOnlyRoute>
              <Login />
            </PublicOnlyRoute>
          }
        />
        <Route
          path="/register"
          element={
            <PublicOnlyRoute>
              <Register />
            </PublicOnlyRoute>
          }
        />
        <Route path="/" element={<HomeRoute />}>
          <Route element={<AppLayout />}><Route index element={<OverviewRoute />} /></Route>
        </Route>
        <Route element={<AppLayout />}>
          <Route path="/paths" element={<LearningPaths />} />
          <Route path="/paths/mine" element={<LearningPaths mine />} />
          <Route path="/paths/:pathId/edit" element={<LearningPathDetail edit />} />
          <Route path="/paths/:pathId" element={<LearningPathDetail />} />
          <Route path="/leaderboard" element={<Leaderboard />} />
          <Route path="/learners/:username" element={<CanonicalProfile />} />
          <Route path="/rooms" element={<BrowseRoute />} />
          <Route path="/rooms/:roomId" element={<BrowseRoute detail />} />
        </Route>
        <Route element={<PrivateRoute />}>
          <Route element={<AppLayout />}>
            <Route path="/profile" element={<ProfileAlias />} />
            <Route path="/settings" element={<AccountSettings />} />
            <Route path="/creator" element={<CreatorWorkspaceRoute />} />
            <Route path="/creator/analytics" element={<CreatorAnalytics />} />
            <Route path="/creator/analytics/rooms/:roomId" element={<CreatorAnalytics />} />
            <Route path="/admin/*" element={<Administration />} />
            <Route path="/saved" element={<Saved />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  )
}

export default App
