import api from './axios'
import type { Question, QuestionList, QuestionWrite, RoomProgress, SubmissionResult } from '../types/api'
export const listQuestions = async (roomId: number): Promise<QuestionList> => (await api.get(`/rooms/${roomId}/questions`)).data
// Read only while an author editor is open; callers keep this out of query caches.
export const getEditableAnswer = async (roomId: number, questionId: number, draft: boolean, revision: number | undefined, signal: AbortSignal): Promise<{answer: string | null; legacy: boolean}> =>
  (await api.get(`/rooms/${roomId}/workspace/questions/${questionId}/answer`, { params: { draft, ...(draft ? { revision } : {}) }, signal })).data
export const createQuestion = async (roomId: number, data: QuestionWrite): Promise<Question> => (await api.post(`/rooms/${roomId}/questions`, data)).data
export const updateQuestion = async (roomId: number, questionId: number, data: QuestionWrite): Promise<Question> => (await api.patch(`/rooms/${roomId}/questions/${questionId}`, data)).data
export const deleteQuestion = async (roomId: number, questionId: number): Promise<void> => { await api.delete(`/rooms/${roomId}/questions/${questionId}`) }
export const orderQuestions = async (roomId: number, questionIds: number[]): Promise<QuestionList> => (await api.put(`/rooms/${roomId}/questions/order`, { question_ids: questionIds })).data
export const getProgress = async (roomId: number): Promise<RoomProgress> => (await api.get(`/rooms/${roomId}/progress`)).data
export const submitAnswer = async (roomId: number, questionId: number, answer: string): Promise<SubmissionResult> => (await api.post(`/rooms/${roomId}/questions/${questionId}/submit`, { answer })).data
// Accepted inputs belong only to the signed-in learner, outside shared caches.
export const getAcceptedAnswers = async (roomId: number, signal: AbortSignal): Promise<import('../types/api').AcceptedAnswer[]> => (await api.get(`/rooms/${roomId}/accepted-answers`, { signal })).data

export const listTasks = async (roomId: number): Promise<import('../types/api').TaskList> => (await api.get(`/rooms/${roomId}/tasks`)).data
export const createTask = async (roomId: number, data: {title: string; instructions: string}): Promise<import('../types/api').RoomTask> => (await api.post(`/rooms/${roomId}/tasks`, data)).data
export const updateTask = async (roomId: number, taskId: number, data: {title: string; instructions: string}): Promise<import('../types/api').RoomTask> => (await api.patch(`/rooms/${roomId}/tasks/${taskId}`, data)).data
export const orderTasks = async (roomId: number, taskIds: number[]): Promise<import('../types/api').TaskList> => (await api.put(`/rooms/${roomId}/tasks/order`, {task_ids: taskIds})).data
export const deleteTask = async (roomId: number, taskId: number): Promise<void> => { await api.delete(`/rooms/${roomId}/tasks/${taskId}`) }
