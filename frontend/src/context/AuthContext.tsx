import { createContext, useContext, useState, useEffect, type ReactNode } from 'react'
import { login as loginApi, getMe, register as registerApi, logoutSession } from '../api/auth'
import { useQueryClient } from '@tanstack/react-query'
import type { User } from '../types/api'
import type { ProfileUpdate } from '../types/api'
import { AvatarImageProvider } from '../components/AvatarImageContext'
import { uploadAvatar as uploadAvatarApi, removeAvatar as removeAvatarApi, updateProfile as updateProfileApi } from '../api/profile'

interface AuthContextType {
  user: User | null
  token: string | null
  login: (username: string, password: string) => Promise<User>
  register: (username: string, email: string, password: string) => Promise<User>
  logout: () => void
  acceptToken: (token: string) => Promise<User>
  updateProfile: (data: ProfileUpdate) => Promise<User>
  uploadAvatar: (file: File) => Promise<User>
  removeAvatar: () => Promise<User>
  isLoading: boolean
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [user, setUser] = useState<User | null>(null)
  const [token, setToken] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    const storedToken = localStorage.getItem('access_token')
    const storedUser = localStorage.getItem('user')

    if (storedToken && storedUser) {
      setToken(storedToken)
      try { setUser(JSON.parse(storedUser)) } catch { localStorage.removeItem('user') }
      getMe()
        .then((user) => {
          setUser(user)
          localStorage.setItem('user', JSON.stringify(user))
        })
        .catch(() => {
          localStorage.removeItem('access_token')
          localStorage.removeItem('user')
          setToken(null)
          setUser(null)
        })
        .finally(() => setIsLoading(false))
    } else {
      setIsLoading(false)
    }
  }, [])

  const acceptToken = async (access_token: string) => {
    queryClient.clear()
    localStorage.setItem('access_token', access_token)
    setToken(access_token)

    const user = await getMe()
    setUser(user)
    localStorage.setItem('user', JSON.stringify(user))
    return user
  }

  const login = async (username: string, password: string) => {
    const { access_token } = await loginApi({ username, password })
    return acceptToken(access_token)
  }

  const register = async (username: string, email: string, password: string) => {
    const user = await registerApi({ username, email, password })
    const { access_token }: { access_token: string } = await loginApi({ username, password })
    queryClient.clear()
    localStorage.setItem('access_token', access_token)
    setToken(access_token)
    setUser(user)
    localStorage.setItem('user', JSON.stringify(user))
    return user
  }

  const logout = () => {
    if (token) void logoutSession(token).catch(() => { /* Local sign-out still clears this browser. */ })
    queryClient.clear()
    localStorage.removeItem('access_token')
    localStorage.removeItem('user')
    setToken(null)
    setUser(null)
  }

  const acceptProfile = (updated: User) => {
    if (localStorage.getItem('access_token') !== token || updated.id !== user?.id) {
      throw new Error('Your session changed. Sign in again before editing your profile.')
    }
    setUser(updated)
    localStorage.setItem('user', JSON.stringify(updated))
    queryClient.setQueryData(['current-user'], updated)
    void queryClient.invalidateQueries({ queryKey: ['leaderboard'] })
    void queryClient.invalidateQueries({ queryKey: ['public-learner'] })
    return updated
  }

  const updateProfile = async (data: ProfileUpdate) => acceptProfile(await updateProfileApi(data))
  const uploadAvatar = async (file: File) => acceptProfile(await uploadAvatarApi(file))
  const removeAvatar = async () => acceptProfile(await removeAvatarApi())

  return (
    <AuthContext.Provider value={{ user, token, login, register, logout, acceptToken, updateProfile, uploadAvatar, removeAvatar, isLoading }}>
      <AvatarImageProvider user={user} token={token}>{children}</AvatarImageProvider>
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
