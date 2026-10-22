import api from './axios'
import type { Room, RoomTemplate } from '../types/api'

export const listRoomTemplates = async (roomId: number): Promise<RoomTemplate[]> => {
  const response = await api.get<RoomTemplate[]>(`/rooms/${roomId}/templates`)
  return response.data
}

export const saveRoomTemplate = async (
  roomId: number,
  idempotencyKey: string,
): Promise<RoomTemplate> => {
  const response = await api.post<RoomTemplate>(`/rooms/${roomId}/templates`, undefined, {
    headers: { 'Idempotency-Key': idempotencyKey },
  })
  return response.data
}

export const publishRoomWithTemplate = async (
  roomId: number,
  templateId: number,
): Promise<Room> => {
  const response = await api.post<Room>(`/rooms/${roomId}/templates/${templateId}/publish`)
  return response.data
}
