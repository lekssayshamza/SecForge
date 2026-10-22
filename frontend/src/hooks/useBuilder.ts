import { hasCreatorAccess } from '../lib/permissions'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { startBuilder, getBuilder, stopBuilder } from '../api/builders'
import { useAuth } from '../context/AuthContext'

const ACTIVE_STATUSES = ['pending', 'provisioning', 'stopping'] as const
type ActiveStatus = (typeof ACTIVE_STATUSES)[number]

const INACTIVE_STATUSES = ['running', 'stopped', 'failed'] as const
type InactiveStatus = (typeof INACTIVE_STATUSES)[number]

export function useBuilder(roomId: number | undefined, enabled = true) {
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const canInspectBuilder = hasCreatorAccess(user)

  const { data: builder, isLoading, error, refetch } = useQuery({
    queryKey: ['builder', roomId],
    queryFn: () => getBuilder(roomId!),
    enabled: !!roomId && roomId > 0 && canInspectBuilder && enabled,
    refetchIntervalInBackground: false,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return false
      return ACTIVE_STATUSES.includes(data.status as ActiveStatus) ? 5000 : false
    },
    retry: (failureCount, error: any) => {
      if (error?.response?.status === 404) return false
      return failureCount < 3
    },
  })

  const startMutation = useMutation({
    mutationFn: (ttlHours?: number) => startBuilder(roomId!, ttlHours),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['builder', roomId] })
    },
  })

  const stopMutation = useMutation({
    mutationFn: () => stopBuilder(roomId!),
    onSuccess: (builder) => {
      queryClient.setQueryData(['builder', roomId], builder)
      return queryClient.invalidateQueries({ queryKey: ['builder', roomId] })
    },
  })

  const isActive = builder ? ACTIVE_STATUSES.includes(builder.status as ActiveStatus) : false
  const isInactive = builder ? INACTIVE_STATUSES.includes(builder.status as InactiveStatus) : false

  return {
    builder,
    isLoading,
    error,
    refetch,
    startBuilder: startMutation.mutate,
    startBuilderAsync: startMutation.mutateAsync,
    isStarting: startMutation.isPending,
    startError: startMutation.error,
    stopBuilderAsync: stopMutation.mutateAsync,
    isStopping: stopMutation.isPending,
    isActive,
    isInactive,
  }
}
