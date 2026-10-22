import type { User } from '../types/api'
// Presentation only. The backend revalidates this capability and ownership.
export function hasCreatorAccess(user: Pick<User, 'role' | 'creator_access'> | null | undefined): boolean {
  return !!user && (user.role === 'creator' || user.role === 'admin')
}

export function hasLearningAccess(user: Pick<User, 'role'> | null | undefined): boolean {
  return !!user && ['learner', 'creator', 'admin'].includes(user.role)
}
