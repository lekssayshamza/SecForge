import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { startLab, getLab, stopLab, extendLab, startTestLab, getTestLab, stopTestLab } from '../api/labs'

export const LAB_TTL_HOURS = 1

const POLL_INTERVAL_MS = 5000

const POLLING_STATUSES = ['pending', 'provisioning', 'stopping'] as const
type PollingStatus = (typeof POLLING_STATUSES)[number]

export function useLab(roomId: number | undefined, enabled: boolean) {
  const state = useLabLifecycle(roomId, enabled)
  return { ...state, startLabAsync: () => state.startLabAsync(undefined) }
}

export function useCreatorTestLab(roomId: number, userId: number) {
  return useLabLifecycle(roomId, true, userId)
}

function useLabLifecycle(roomId: number | undefined, enabled: boolean, creatorId?: number) {
  const queryClient = useQueryClient()
  const isTestLab = creatorId !== undefined
  const queryKey = isTestLab ? ['creator-test-lab', roomId, creatorId] : ['lab', roomId]

  const { data: lab, isLoading, isFetching, error, refetch } = useQuery({
    queryKey,
    queryFn: () => isTestLab ? getTestLab(roomId!) : getLab(roomId!),
    enabled: !!roomId && roomId > 0 && enabled,
    refetchIntervalInBackground: false,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return false
      if (POLLING_STATUSES.includes(data.status as PollingStatus)) return POLL_INTERVAL_MS
      return !isTestLab && data.status === 'running' ? 15000 : false
    },
    retry: (failureCount, error: any) => {
      const status = error?.response?.status
      if (status === 404 || status === 403 || status === 409) return false
      return failureCount < 3
    },
  })

  const startMutation = useMutation({
    mutationFn: (templateId: number | undefined) => {
      if (isTestLab) {
        if (!templateId) throw new Error('Select a saved version before launching')
        return startTestLab(roomId!, templateId, LAB_TTL_HOURS)
      }
      return startLab(roomId!, LAB_TTL_HOURS)
    },
    onSuccess: (lab) => {
      queryClient.setQueryData(queryKey, lab)
      return queryClient.invalidateQueries({ queryKey })
    },
  })

  const stopMutation = useMutation({
    mutationFn: () => isTestLab ? stopTestLab(roomId!) : stopLab(roomId!),
    onSuccess: (lab) => {
      queryClient.setQueryData(queryKey, lab)
      return queryClient.invalidateQueries({ queryKey })
    },
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey })

  const extension = useMutation({
    mutationFn: () => {
      if (isTestLab || !lab) throw new Error('No learner session to extend')
      return extendLab(roomId!, lab)
    },
    onSuccess: updated => { queryClient.setQueryData(queryKey, updated); return queryClient.invalidateQueries({ queryKey }) },
    onError: () => { void queryClient.invalidateQueries({ queryKey }) },
    retry: false,
  })

  return {
    lab,
    isLoading,
    isFetching,
    error,
    refetch,
    refresh,
    startLabAsync: startMutation.mutateAsync,
    isStarting: startMutation.isPending,
    stopLabAsync: stopMutation.mutateAsync,
    isStopping: stopMutation.isPending,
    extendLabAsync: extension.mutateAsync,
    isExtending: extension.isPending,
  }
}
