import api from './axios'
import type { Builder } from '../types/api'

export const startBuilder = async (roomId: number, ttlHours = 4): Promise<Builder> => {
  const response = await api.post<Builder>(`/rooms/${roomId}/builder`, { ttl_hours: ttlHours })
  return response.data
}

export const stopBuilder = async (roomId: number): Promise<Builder> => {
  const response = await api.post<Builder>(`/rooms/${roomId}/builder/stop`)
  return response.data
}

export const getBuilder = async (roomId: number): Promise<Builder | null> => {
  try {
    const response = await api.get<Builder>(`/rooms/${roomId}/builder`)
    return response.data
  } catch (error: any) {
    if (error.response?.status === 404) {
      return null
    }
    throw error
  }
}
