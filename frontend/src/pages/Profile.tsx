import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { useLearning } from '../hooks/useLearning'
import { getActivity } from '../api/profile'
import Avatar from '../components/Avatar'
import SocialConnections from '../components/SocialConnections'
import { Achievements, LearningRooms, ActivityList } from '../components/LearningPanels'
import Dialog from '../components/Dialog'
import ProfileIdentity, { ProfileMetrics } from '../components/ProfileIdentity'
import { actionError } from '../lib/presentation'
import type { ProfileUpdate } from '../types/api'
export default function Profile() {
  const { user } = useAuth(), summary = useLearning()
  const navigate = useNavigate(), location = useLocation()
  const [editing, setEditing] = useState(false), [page, setPage] = useState(1)
  const history = useQuery({ queryKey: ['learning-activity', user?.id, page], queryFn: () => getActivity(page), enabled: Boolean(user), staleTime: 30_000, retry: false })
  if (!user) return null
  return <div className="workspace profile-v2"><ProfileIdentity person={user} label="Your profile"><div className="profile-header-actions"><button className="btn btn-secondary" onClick={() => setEditing(true)}>Edit Profile</button><Link to="/settings">Account settings</Link></div></ProfileIdentity>
    <SocialConnections username={user.username} />
    {summary.isPending ? <p className="loading" role="status">Loading your accomplishments…</p> : summary.isError || !summary.data ? <div className="error" role="alert">Learning statistics are unavailable. <button className="btn btn-ghost" onClick={() => void summary.refetch()}>Try again</button></div> : <><ProfileMetrics stats={summary.data.stats} /><Achievements badges={summary.data.achievements} /><section className="panel learning-section"><h2>Recently completed rooms</h2><LearningRooms rooms={summary.data.completed_rooms} completed empty="No completed rooms yet. Finish a challenge to record your accomplishment." /></section></>}
    <section className="panel learning-section"><h2>Learning history</h2>{history.isPending ? <p role="status">Loading activity…</p> : history.isError || !history.data ? <div className="error" role="alert">Activity is unavailable. <button className="btn btn-ghost" onClick={() => void history.refetch()}>Try again</button></div> : <><ActivityList items={history.data.items} />{history.data.total > history.data.page_size && <nav className="public-pagination" aria-label="Activity pages"><button className="btn btn-secondary" disabled={page === 1 || history.isFetching} onClick={() => setPage(p => p - 1)}>Previous</button><span>Page {page}</span><button className="btn btn-secondary" disabled={page >= 1000 || page * history.data.page_size >= history.data.total || history.isFetching} onClick={() => setPage(p => p + 1)}>Next</button></nav>}</>}</section>

    {editing && <ProfileEditor onClose={() => setEditing(false)} onIdentitySaved={username => { if (location.pathname.startsWith('/learners/')) navigate(`/learners/${encodeURIComponent(username)}`, { replace: true }) }} />}
  </div>
}

export function ProfileEditor({ onClose, onIdentitySaved }: { onClose: () => void; onIdentitySaved?: (username: string) => void }) {
  const { user, updateProfile, uploadAvatar, removeAvatar } = useAuth()
  const [username, setUsername] = useState(user?.username ?? ''), [displayName, setDisplayName] = useState(user?.display_name ?? ''), [bio, setBio] = useState(user?.bio ?? '')
  const [avatarPublic, setAvatarPublic] = useState(user?.avatar_public ?? false), [bioPublic, setBioPublic] = useState(user?.bio_public ?? false)
  const [file, setFile] = useState<File | null>(null), [previewUrl, setPreviewUrl] = useState<string | undefined>()
  const [pending, setPending] = useState(false), [error, setError] = useState(''), [saved, setSaved] = useState(''); const operation = useRef(false)
  useEffect(() => { if (!file) { setPreviewUrl(undefined); return } const url = URL.createObjectURL(file); setPreviewUrl(url); return () => URL.revokeObjectURL(url) }, [file])
  if (!user) return null
  const preview = { ...user, username: username.trim() || user.username, display_name: displayName.trim() }
  const changed = username !== user.username || displayName.trim() !== (user.display_name ?? '') || bio.trim() !== (user.bio ?? '') || avatarPublic !== Boolean(user.avatar_public) || bioPublic !== Boolean(user.bio_public)
  const perform = async (action: () => Promise<unknown>, message: string) => {
    if (operation.current) return
    operation.current = true; setPending(true); setError(''); setSaved('')
    try { await action(); setSaved(message) } catch (err: any) { setError(err?.response?.status === 422 && typeof err?.response?.data?.detail === 'string' ? err.response.data.detail : err?.response?.status === 409 ? 'That username is already in use.' : actionError(err, 'save your profile')) }
    finally { operation.current = false; setPending(false) }
  }
  const save = async (event: FormEvent) => {
    event.preventDefault(); if (!changed) return
    const data: ProfileUpdate = { display_name: displayName.trim() || null, bio: bio.trim() || null, avatar_public: avatarPublic, bio_public: bioPublic }
    if (username !== user.username) data.username = username
    await perform(async () => { const updated = await updateProfile(data); setUsername(updated.username); setDisplayName(updated.display_name ?? ''); setBio(updated.bio ?? ''); onIdentitySaved?.(updated.username) }, 'Profile saved. Your identity has been updated throughout SecForge.')
  }
  return <Dialog title="Edit your profile" onClose={onClose} busy={pending}><div className="profile-editor-body">
    {error && <div className="error" role="alert">{error}</div>}{saved && <p className="profile-success" role="status">{saved}</p>}
    <section className="picture-editor" aria-label="Profile picture"><Avatar user={preview} large imageUrl={previewUrl} /><div><label htmlFor="profile-picture">Upload a picture</label><input id="profile-picture" type="file" accept="image/jpeg,image/png,image/webp" disabled={pending} onChange={e => { const chosen = e.target.files?.[0] ?? null; setSaved(''); if (chosen && chosen.size > 2 * 1024 * 1024) { setError('Picture must be at most 2 MB'); setFile(null); e.target.value = '' } else { setError(''); setFile(chosen) } }} /><p className="form-hint">JPEG, PNG or WebP · 2 MB max · 16–2048 pixels per side. Pictures are saved separately.</p><div className="public-room-actions">{file && <button type="button" className="btn btn-secondary btn-sm" disabled={pending} onClick={() => void perform(async () => { await uploadAvatar(file); setFile(null) }, 'Picture saved.')}>Save picture</button>}{user.avatar_version && <button type="button" className="btn btn-ghost btn-sm" disabled={pending} onClick={() => void perform(async () => { await removeAvatar(); setFile(null) }, 'Picture removed. Your default avatar is restored.')}>Remove picture</button>}</div></div></section>
    <form onSubmit={event => void save(event)}><fieldset className="profile-fields" disabled={pending}>
      <div className="form-group"><label htmlFor="display-name">Display name</label><input id="display-name" value={displayName} onChange={e => setDisplayName(e.target.value)} maxLength={80} autoComplete="nickname" /></div>
      <div className="form-group"><label htmlFor="profile-username">Username</label><input id="profile-username" value={username} onChange={e => setUsername(e.target.value)} required maxLength={50} autoComplete="username" aria-describedby="username-help" /><p id="username-help" className="form-hint">3–50 letters, numbers, dots, underscores or hyphens. Your identity, VPN credentials and progress remain unchanged.</p></div>
      <div className="form-group"><label htmlFor="profile-bio">Bio</label><textarea id="profile-bio" rows={3} maxLength={280} value={bio} onChange={e => setBio(e.target.value)} /><p className="form-hint">{bio.length}/280 characters</p></div>
      <fieldset className="profile-privacy"><legend>Public profile</legend><label className="checkbox-label"><input type="checkbox" checked={avatarPublic} onChange={e => setAvatarPublic(e.target.checked)} /> Show my uploaded picture publicly</label><label className="checkbox-label"><input type="checkbox" checked={bioPublic} onChange={e => setBioPublic(e.target.checked)} /> Show my bio publicly</label><details><summary>About visibility</summary><p className="form-hint">Off by default. These settings apply wherever your public learner profile appears. Your email and sign-in settings are never public. Visitors can save pictures you make public.</p></details></fieldset>
    </fieldset><footer className="profile-actions"><button type="submit" className="btn btn-primary" disabled={pending || !changed}>{pending ? 'Saving…' : 'Save profile'}</button><button type="button" className="btn btn-ghost" disabled={pending} onClick={onClose}>Done</button></footer></form>
  </div></Dialog>
}
