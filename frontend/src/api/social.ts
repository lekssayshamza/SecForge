import api from './axios'
import type { PublicIdentity } from '../types/api'
export interface Connections { followers: number; following: number }
export interface Relationship extends Connections { following_user: boolean; blocked: boolean; can_follow: boolean; is_self: boolean }
export interface ConnectionPage { items: PublicIdentity[]; total: number; page: number; page_size: number }
const name = (username: string) => encodeURIComponent(username)
export const getConnections = async (username: string): Promise<Connections> => (await api.get(`/public/learners/${name(username)}/connections`)).data
export const getRelationship = async (username: string): Promise<Relationship> => (await api.get(`/social/users/${name(username)}`)).data
export const listConnections = async (username: string, direction: 'followers' | 'following', page: number): Promise<ConnectionPage> => (await api.get(`/public/learners/${name(username)}/connections/${direction}`, { params: { page, page_size: 20 } })).data
export const setRelationship = async (username: string, action: 'follow' | 'block', enabled: boolean): Promise<Relationship> => (await api.request({ url: `/social/users/${name(username)}/${action}`, method: enabled ? 'PUT' : 'DELETE' })).data
