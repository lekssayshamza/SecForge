import type { Room } from '../types/api'

// Do not render raw server diagnostics: they can contain infrastructure or credential details.
export function actionError(error: unknown, action: string): string {
  const status = (error as { response?: { status?: number } })?.response?.status
  if (status === 409 && action.includes('delete') && action.includes('room') &&
      (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail === 'Rooms with learner progress cannot be deleted.') {
    return 'This room contains learner progress and cannot be deleted. Unpublish it to remove it from discovery.'
  }
  if (status === 409 && action.includes('delete') && action.includes('room') &&
      (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail === 'This room has retained publication or machine history. Unpublish it instead; its images and resource records are preserved.') {
    return 'This room has retained publication or machine history. Unpublish it to remove it from discovery; images and recovery records are preserved.'
  }
  if (status === 401) return 'Your session has expired. Sign in again.'
  if (status === 403) return 'Your account does not have permission for this action.'
  if (status === 404) return 'This resource is no longer available. Refresh the page.'
  if (status === 409) return 'The resource state changed or another environment is active. Refresh status before trying again.'
  if (status === 422) return 'Check the required fields and try again.'
  if (status === 503 || status === 502) return 'The service is temporarily unavailable. Refresh status before retrying.'
  return `Unable to ${action}. Check your connection and refresh status before retrying.`
}

export function filterRooms(rooms: Room[], search: string, difficulty: string, status: string, category: string) {
  const query = search.trim().toLocaleLowerCase()
  return rooms.filter(room => (!query || `${room.name} ${room.description ?? ''} ${(room.categories?.length ? room.categories : [room.category]).join(' ')}`.toLocaleLowerCase().includes(query))
    && (!difficulty || room.difficulty === difficulty) && (!status || room.status === status)
    && (!category || (room.categories?.length ? room.categories : [room.category]).includes(category)))
}

export const isImageReady = (machines: { status: string; ami_id: string | null }[]) =>
  machines.some(machine => machine.status === 'available' && !!machine.ami_id?.trim())
