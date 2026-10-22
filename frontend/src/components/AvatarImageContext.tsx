import { createContext, useEffect, useState, type ReactNode } from 'react'
import { loadAvatar } from '../api/profile'
import type { User } from '../types/api'
export const AvatarImageContext = createContext<{ id: number; version: string; url: string } | null>(null)
export function AvatarImageProvider({ user, token, children }: { user: User | null; token: string | null; children: ReactNode }) {
  const [image, setImage] = useState<{ id: number; version: string; url: string } | null>(null)
  const id = user?.id, version = user?.avatar_version
  useEffect(() => {
    setImage(null)
    if (!id || !version || !token) return
    const controller = new AbortController(); let alive = true, url: string | undefined
    void loadAvatar(controller.signal).then(blob => {
      if (!alive || blob.type !== 'image/png') return
      url = URL.createObjectURL(blob); setImage({ id, version, url })
    }).catch(() => { /* Keep the generated fallback; auth failures use the existing interceptor. */ })
    return () => { alive = false; controller.abort(); if (url) URL.revokeObjectURL(url) }
  }, [id, version, token])
  return <AvatarImageContext.Provider value={image}>{children}</AvatarImageContext.Provider>
}
