import api from './axios'
import type { LoginRequest, TokenResponse, User } from '../types/api'

export const login = async (credentials: LoginRequest): Promise<TokenResponse> => {
  const response = await api.post<TokenResponse>('/auth/login', credentials)
  return response.data
}

export const getMe = async (): Promise<User> => {
  const response = await api.get<User>('/auth/me')
  return response.data
}

export interface RegisterRequest {
  username: string
  email: string
  password: string
}

export const register = async (data: RegisterRequest): Promise<User> => {
  const response = await api.post<User>('/users', data)
  return response.data
}

export interface AuthOptions { google_enabled: boolean; registration_enabled: boolean; password_recovery_enabled: boolean }
export const getAuthOptions = async (): Promise<AuthOptions> => (await api.get('/auth/options')).data
export const googleStartUrl = () => `${api.defaults.baseURL}/auth/google/start`
export const changePassword = async (current_password: string, new_password: string): Promise<TokenResponse> =>
  (await api.post('/auth/password', { current_password, new_password })).data
export const logoutSession = async (token: string) => api.post('/auth/logout', {}, { headers: { Authorization: `Bearer ${token}` } })
export const getGoogleStatus = async (): Promise<{ choose_username: boolean; linking: boolean }> =>
  (await api.get('/auth/google/status', { withCredentials: true })).data
export const completeGoogle = async (username?: string): Promise<TokenResponse> =>
  (await api.post('/auth/google/complete', username ? { username } : {}, { withCredentials: true })).data
export const linkGoogle = async (password: string): Promise<{ authorization_url: string }> =>
  (await api.post('/auth/google/link', { password }, { withCredentials: true })).data
