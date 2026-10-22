import api from './axios'
import type { Lab } from '../types/api'
import { stampLabTime } from '../lib/labClock'

export const startLab = async (roomId: number, ttlHours: number): Promise<Lab> => {
  const response = await api.post<Lab>(`/rooms/${roomId}/lab`, { ttl_hours: ttlHours })
  return stampLabTime(response.data)
}

export const getLab = async (roomId: number): Promise<Lab | null> => {
  try {
    const response = await api.get<Lab>(`/rooms/${roomId}/lab`)
    return stampLabTime(response.data)
  } catch (error: any) {
    if (error.response?.status === 404) {
      return null
    }
    throw error
  }
}

export const stopLab = async (roomId: number): Promise<Lab> => {
  const response = await api.post<Lab>(`/rooms/${roomId}/lab/stop`)
  return stampLabTime(response.data)
}

export const startTestLab = async (roomId: number, templateId: number, ttlHours: number): Promise<Lab> => {
  const response = await api.post<Lab>(`/rooms/${roomId}/templates/${templateId}/lab`, { ttl_hours: ttlHours })
  return stampLabTime(response.data)
}

export const getTestLab = async (roomId: number): Promise<Lab | null> => {
  try {
    return stampLabTime((await api.get<Lab>(`/rooms/${roomId}/test-lab`)).data)
  } catch (error: any) {
    if (error.response?.status === 404) return null
    throw error
  }
}

export const stopTestLab = async (roomId: number): Promise<Lab> => {
  return stampLabTime((await api.post<Lab>(`/rooms/${roomId}/test-lab/stop`)).data)
}

export const extendLab = async (roomId: number, lab: Lab): Promise<Lab> => {
  return stampLabTime((await api.post<Lab>(`/rooms/${roomId}/lab/extend`, {
    lab_id: lab.id, expected_extensions_used: lab.extensions_used ?? 0,
  })).data)
}
