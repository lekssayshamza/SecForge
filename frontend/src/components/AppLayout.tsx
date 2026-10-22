import { Outlet } from 'react-router-dom'
export default function AppLayout() {
  return <div className="app-shell"><main id="main-content" tabIndex={-1}><Outlet /></main></div>
}
