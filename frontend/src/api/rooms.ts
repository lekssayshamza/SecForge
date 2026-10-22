import api from './axios'
import type { Room, RoomCreate, RoomUpdate, PublicRoom, PublicRoomPage } from '../types/api'

export const listPublicRooms = async (params: { q?: string; category?: string; difficulty?: string; room_type?: string; page?: number }): Promise<PublicRoomPage> =>
  (await api.get('/public/rooms', { params })).data
export const getPublicRoom = async (roomId: number): Promise<PublicRoom> =>
  (await api.get(`/public/rooms/${roomId}`)).data

export const listRooms = async (): Promise<Room[]> => {
  const response = await api.get<Room[]>('/rooms')
  return response.data
}

export const getRoom = async (roomId: number): Promise<Room> => {
  const response = await api.get<Room>(`/rooms/${roomId}`)
  return response.data
}

export const createRoom = async (roomData: RoomCreate): Promise<Room> => {
  const response = await api.post<Room>('/rooms', roomData)
  return response.data
}

export const updateRoom = async (roomId: number, roomData: RoomUpdate): Promise<Room> => {
  const response = await api.patch<Room>(`/rooms/${roomId}`, roomData)
  return response.data
}

export const deleteRoom = async (roomId: number): Promise<void> => {
  await api.delete(`/rooms/${roomId}`)
}

export const publishRoom = async (roomId: number): Promise<Room> => {
  const response = await api.post<Room>(`/rooms/${roomId}/publish`)
  return response.data
}

export const unpublishRoom = async (roomId: number): Promise<Room> => {
  const response = await api.post<Room>(`/rooms/${roomId}/unpublish`)
  return response.data
}
