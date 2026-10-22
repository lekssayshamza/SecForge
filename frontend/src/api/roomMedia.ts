import api from './axios'
export interface LessonImage { id: string; room_id: number; url: string; media_type: string; width: number; height: number; byte_size: number; created_at: string; in_use: boolean }
export interface LessonImages { items: LessonImage[]; total: number; page: number; page_size: number }
export const listLessonImages = async (room: number, page = 1) => (await api.get<LessonImages>(`/rooms/${room}/media`, { params: { page } })).data
export const uploadLessonImage = async (room: number, file: File, signal: AbortSignal) => (await api.post<LessonImage>(`/rooms/${room}/media`, file, { headers: { 'Content-Type': file.type }, timeout: 35000, signal })).data
export const removeLessonImage = async (room: number, id: string) => { await api.delete(`/rooms/${room}/media/${id}`) }
