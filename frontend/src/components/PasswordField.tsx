import { useState } from 'react'

interface Props { id: string; label: string; value: string; onChange: (value: string) => void; disabled?: boolean; newPassword?: boolean }
export default function PasswordField({ id, label, value, onChange, disabled, newPassword }: Props) {
  const [visible, setVisible] = useState(false)
  return <div className="form-group"><label htmlFor={id}>{label}</label><div className="password-field">
    <input id={id} type={visible ? 'text' : 'password'} value={value} onChange={e => onChange(e.target.value)} required
      disabled={disabled} autoComplete={newPassword ? 'new-password' : 'current-password'} minLength={newPassword ? 12 : undefined} maxLength={128} />
    <button type="button" aria-label={`${visible ? 'Hide' : 'Show'} ${label.toLowerCase()}`} aria-pressed={visible} disabled={disabled} onClick={() => setVisible(!visible)}>{visible ? 'Hide' : 'Show'}</button>
  </div></div>
}
