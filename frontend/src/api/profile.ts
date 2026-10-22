import api from './axios'
import type { ProfileUpdate, User, LearningOverview, ActivityPage } from '../types/api'
export const updateProfile = async (data: ProfileUpdate): Promise<User> => (await api.patch<User>('/users/me', data)).data
export const getLearning = async (): Promise<LearningOverview> => (await api.get<LearningOverview>('/users/me/learning')).data
export const getActivity = async (page: number): Promise<ActivityPage> => (await api.get<ActivityPage>('/users/me/activity', { params: { page, page_size: 20 } })).data
export const uploadAvatar = async (file: File): Promise<User> => (await api.put<User>('/users/me/avatar', file, { headers: { 'Content-Type': file.type } })).data
export const removeAvatar = async (): Promise<User> => (await api.delete<User>('/users/me/avatar')).data
export const loadAvatar = async (signal: AbortSignal): Promise<Blob> => (await api.get<Blob>('/users/me/avatar', { responseType: 'blob', signal })).data
