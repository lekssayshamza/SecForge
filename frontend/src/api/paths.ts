import api from './axios'
import type { PublicRoom } from '../types/api'
export interface PathWrite { title: string; description: string; difficulty: string; category: string; objectives: string }
export interface LearningPath extends PathWrite { id: number; status: string; room_count: number }
export type PathRoom = PublicRoom & { publication_status?: 'draft' | 'published' }
export interface PathDetail extends LearningPath { rooms: PathRoom[]; unavailable_count: number }
export interface PathPage { items: LearningPath[]; total: number; page: number; page_size: number; categories: string[] }
export interface PathProgress { completed_room_ids: number[]; rooms_total: number; rooms_completed: number; percent: number; next_room_id: number | null; completed: boolean }
export const listPaths = async (params: { q?: string; category?: string; difficulty?: string; page?: number }, mine = false): Promise<PathPage> => (await api.get(mine ? '/paths' : '/public/paths', { params: mine ? { page: params.page } : params })).data
export const getPath = async (id: number, owned = false): Promise<PathDetail> => (await api.get(`${owned ? '' : '/public'}/paths/${id}`)).data
export const createPath = async (data: PathWrite): Promise<PathDetail> => (await api.post('/paths', data)).data
export const updatePath = async (id: number, data: PathWrite & { room_ids?: number[] }): Promise<PathDetail> => (await api.patch(`/paths/${id}`, data)).data
export const savePathRooms = async (id: number, room_ids: number[]): Promise<PathDetail> => (await api.put(`/paths/${id}/rooms`, { room_ids })).data
export const publishPath = async (id: number, unpublish = false): Promise<PathDetail> => (await api.post(`/paths/${id}/${unpublish ? 'unpublish' : 'publish'}`)).data
export const deletePath = async (id: number): Promise<void> => { await api.delete(`/paths/${id}`) }
export const getPathProgress = async (id: number): Promise<PathProgress> => (await api.get(`/paths/${id}/progress`)).data
export const eligibleRooms = async (q: string, page: number, category = '', difficulty = ''): Promise<{ items: PathRoom[]; has_more: boolean }> => (await api.get('/paths/eligible-rooms', { params: { q, page, category, difficulty } })).data
