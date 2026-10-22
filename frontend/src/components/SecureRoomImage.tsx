import { useEffect, useState } from 'react'
import { useAuth } from '../context/AuthContext'
import api from '../api/axios'
export const roomImageSource = (src: string) => /^\/api\/rooms\/[1-9][0-9]*\/media\/[a-f0-9]{32}$/.test(src)
/** Private images never place credentials in a URL or fetch third-party hosts. */
export default function SecureRoomImage({ src, alt }: { src: string; alt: string }) {
  const { token } = useAuth()
  const [image, setImage] = useState<{ source: string; token: string; url: string } | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    setImage(null); setFailed(false)
    if (!token || !roomImageSource(src)) return
    const controller = new AbortController(); let alive = true, url: string | undefined
    void api.get<Blob>(src.slice(4), { responseType: 'blob', signal: controller.signal }).then(({ data }) => {
      if (!alive) return
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(data.type) || data.size > 2621440) throw new Error('Image unavailable')
      url = URL.createObjectURL(data); setImage({ source: src, token, url })
    }).catch(() => { if (alive) setFailed(true) })
    return () => { alive = false; controller.abort(); if (url) URL.revokeObjectURL(url) }
  }, [src, token])
  const current = image?.source === src && image.token === token ? image : null
  return current ? <img className="lesson-image" src={current.url} alt={alt} decoding="async" /> : <span className="lesson-image-placeholder" role="status">{!token ? 'Sign in to view lesson images.' : failed ? `Image unavailable: ${alt}` : `Loading image: ${alt}`}</span>
}
