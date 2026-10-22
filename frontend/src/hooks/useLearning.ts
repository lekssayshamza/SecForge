import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { getLearning } from '../api/profile'
export function useLearning() {
  const { user } = useAuth()
  return useQuery({ queryKey: ['learning', user?.id], queryFn: getLearning, enabled: Boolean(user), staleTime: 30_000, retry: false })
}
