import axios from 'axios'

// An explicitly empty production value selects the same-origin /api proxy.
const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export const api = axios.create({
  baseURL: `${apiBaseUrl}/api`,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Request interceptor to attach JWT
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Response interceptor for auth errors
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const url: string = error.config?.url || ''
    const detail = error.response?.data?.detail
    // A rejected current password keeps the form open. An expired/revoked
    // Bearer session on the same endpoint must still clear the session.
    const rejectedReauthentication =
      (url === '/auth/password' && detail === 'Current password is incorrect') ||
      (url === '/auth/google/link' && detail === 'Reauthenticate with your current password')
    const guestAuthentication = ['/auth/login', '/auth/google/status', '/auth/google/complete'].includes(url)
    if (error.response?.status === 401 && !guestAuthentication && !rejectedReauthentication) {
      localStorage.removeItem('access_token')
      localStorage.removeItem('user')
      // An expired remembered session must not turn the public home page
      // into a login redirect. AuthProvider clears its state after getMe fails.
      const path = window.location.pathname
      const publicHomeSessionCheck = url === '/auth/me' && (['/', '/login', '/register', '/auth/google', '/rooms'].includes(path) || /^\/rooms\/[1-9][0-9]*$/.test(path))
      if (!publicHomeSessionCheck) window.location.href = '/login'
    }
    return Promise.reject(error)
  }
)

export default api
