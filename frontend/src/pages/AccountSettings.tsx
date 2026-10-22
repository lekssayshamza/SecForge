import { Link } from 'react-router-dom'
import SignInMethods from '../components/SignInMethods'
import Icon from '../components/Icon'
export default function AccountSettings() {
  return <div className="workspace account-settings-page"><header className="page-toolbar"><div><p className="eyebrow">Your account</p><h1>Account settings</h1><p className="muted">Manage how you sign in to SecForge.</p></div><Link className="btn btn-secondary" to="/profile"><Icon name="grid" size={16} /> My Profile</Link></header><SignInMethods /><details className="panel settings-help"><summary>Account security</summary><p>Google linking is optional. Keep using your password if you prefer. Google-only accounts sign in through Google; no separate password is required.</p><p>Changing your password signs out other sessions. Never share passwords or VPN profiles.</p></details></div>
}
