import api from './axios'
export interface CreatorKey { configured: boolean; revision: number; enabled: boolean; fingerprint: string | null; public_key: string | null; applied_builder_id: number | null; applied_at: string | null; pending: boolean }
export const getCreatorKey = async () => (await api.get<CreatorKey>('/users/me/creator-key')).data
export const saveCreatorKey = async (public_key: string, revision: number) => (await api.put<CreatorKey>('/users/me/creator-key', { public_key, revision })).data
export const removeCreatorKey = async (revision: number) => (await api.delete<CreatorKey>('/users/me/creator-key', { data: { revision } })).data
export const applyCreatorKey = async (room: number) => (await api.post<CreatorKey>(`/rooms/${room}/builder/creator-key`, {}, { timeout: 120000 })).data
export const adminCreatorKey = async (id: number) => (await api.get<CreatorKey>(`/admin/users/${id}/creator-key`)).data
export const revokeCreatorKey = async (id: number, revision: number, confirm_username: string, reason: string) => (await api.post<CreatorKey>(`/admin/users/${id}/creator-key/revoke`, { revision, confirm_username, reason }, { timeout: 120000 })).data
