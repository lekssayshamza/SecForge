import api from './axios'
import type { PublicRoom } from '../types/api'
import type { LearningPath } from './paths'
export type ContentKind = 'rooms' | 'paths'
export interface RoomCard extends PublicRoom { points_total?: number | null; bookmarked?: boolean; progress?: 'not-started' | 'in-progress' | 'completed' }
export interface PathCard extends LearningPath { bookmarked?: boolean; rooms_completed?: number; percent?: number }
export interface CatalogPage<T> { items: T[]; total: number; page: number; page_size: number; categories: string[] }
export interface DiscoveryParams { q?: string; category?: string; difficulty?: string; room_type?: string; progress?: string; sort?: string; page?: number }
export const discover = async (kind: ContentKind, params: DiscoveryParams, personal: boolean, saved: boolean, signal?: AbortSignal): Promise<CatalogPage<RoomCard | PathCard>> =>
  (await api.get(saved ? `/saved/${kind}` : `${personal ? '/discovery' : '/public'}/${kind}`, { params: saved ? { q: params.q, page: params.page } : { ...params, progress: personal && kind === 'rooms' ? params.progress : undefined, room_type: kind === 'rooms' ? params.room_type : undefined }, signal })).data
export const bookmarkState = async (kind: ContentKind, id: number): Promise<{ bookmarked: boolean }> => (await api.get(`/bookmarks/${kind}/${id}`)).data
export const setBookmark = async (kind: ContentKind, id: number, saved: boolean): Promise<{ bookmarked: boolean }> => (await api[saved ? 'put' : 'delete'](`/bookmarks/${kind}/${id}`)).data
