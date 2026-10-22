import Icon from './Icon'
import { useContext, useState } from 'react'
import { AvatarImageContext } from './AvatarImageContext'
import type { User } from '../types/api'

export const avatarStyles = ['initials', 'terminal', 'compass', 'code', 'shield', 'network'] as const
export const avatarColors = ['red', 'navy', 'green', 'blue', 'purple'] as const

export default function Avatar({ user, large = false, decorative = false, imageUrl }: { user: Pick<User, 'username' | 'display_name' | 'avatar_style' | 'avatar_color'> & Partial<Pick<User, 'id' | 'avatar_version'>> & { avatar_url?: string | null }; large?: boolean; decorative?: boolean; imageUrl?: string | null }) {
  const image = useContext(AvatarImageContext)
  const candidate = imageUrl === undefined ? (image && image.id === user.id && image.version === user.avatar_version ? image.url : user.avatar_url) : imageUrl
  const picture = candidate && (candidate.startsWith('blob:') || /^\/api\/public\/learners\/[a-zA-Z0-9_.-]{3,50}\/avatar(?:\?v=[a-f0-9]{16})?$/.test(candidate)) ? candidate : null
  const [failed, setFailed] = useState<string | null>(null)
  const style = avatarStyles.find(value => value === user.avatar_style) ?? 'initials'
  const color = avatarColors.find(value => value === user.avatar_color) ?? 'red'
  const name = user.display_name || user.username
  const words = name.trim().split(/\s+/)
  const initials = words.length > 1 ? Array.from(words[0])[0] + Array.from(words[words.length - 1])[0] : Array.from(name).slice(0, 2).join('')
  return <span className={`avatar avatar-${color}${large ? ' avatar-large' : ''}`} role={decorative ? undefined : 'img'} aria-hidden={decorative || undefined} aria-label={decorative ? undefined : `Profile picture for ${name}`}>
    {picture && failed !== picture ? <img src={picture} alt="" aria-hidden="true" onError={() => setFailed(picture)} /> : style === 'initials' ? initials.toUpperCase() : <Icon name={style} size={large ? 32 : 17} />}
  </span>
}
