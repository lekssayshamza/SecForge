import api from './axios'
import type { Question, Room, RoomTask } from '../types/api'

export type WorkspaceAction = 'metadata' | 'question_create' | 'question_update' | 'question_remove' | 'question_order' | 'task_create' | 'task_update' | 'task_remove' | 'task_order'
export interface RoomWorkspace {
  room: Room
  revision: number
  dirty: boolean
  questions: (Question & { has_saved_answer: boolean })[]
  tasks: RoomTask[]
  image_status: string | null
  image_template_id: number | null
  image_is_published: boolean
}
export interface DraftEditor {
  data: RoomWorkspace
  write: (action: WorkspaceAction, data: object, revision?: number) => Promise<void>
  refresh: () => Promise<unknown>
}
export const getWorkspace = async (id: number): Promise<RoomWorkspace> => (await api.get(`/rooms/${id}/workspace`)).data
export const writeWorkspace = async (id: number, revision: number, action: WorkspaceAction, data: object): Promise<RoomWorkspace> => (await api.patch(`/rooms/${id}/workspace`, { revision, action, data })).data
export const publishWorkspace = async (id: number, revision: number): Promise<RoomWorkspace> => (await api.post(`/rooms/${id}/workspace/publish`, { revision })).data
