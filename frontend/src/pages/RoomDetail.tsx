import { hasCreatorAccess, hasLearningAccess } from '../lib/permissions'
import { useRef, useState } from 'react'
import { useParams, useNavigate, useLocation, Link } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useQuery, useMutation, useQueryClient, useIsFetching, useIsMutating } from '@tanstack/react-query'
import { getRoom, deleteRoom, unpublishRoom } from '../api/rooms'
import { listRoomTemplates, saveRoomTemplate } from '../api/templates'
import { getWorkspace, writeWorkspace, publishWorkspace, type DraftEditor } from '../api/workspace'
import BuilderSshAuth from '../components/BuilderSshAuth'
import NetworkPorts from '../components/NetworkPorts'
import WorkspaceShell from '../components/WorkspaceShell'
import { formatDateTime } from '../lib/format'
import { newIdempotencyKey, shouldRetainCaptureKey, shouldReuseCaptureKey } from '../lib/idempotency'
import { useBuilder } from '../hooks/useBuilder'
import { useLab, useCreatorTestLab } from '../hooks/useLab'
import { downloadVpnProfile } from '../api/vpn'
import Icon from '../components/Icon'
import CategoryMultiSelect from '../components/CategoryMultiSelect'
import BookmarkButton from '../components/BookmarkButton'
import { categoryLabel } from '../lib/categories'
import { discoveryReturn } from '../hooks/useDiscoveryState'
import Dialog from '../components/Dialog'
import ConnectionGuide from '../components/ConnectionGuide'
import { safeProfileFilename, vpnProfileFilename } from '../lib/vpnFilename'
import EnvironmentStatus from '../components/EnvironmentStatus'
import LabTimer, { useLabClock } from '../components/LabTimer'
import ChallengeQuestions from '../components/ChallengeQuestions'
import RoomLearningProgress from '../components/RoomLearningProgress'
import { useUnsavedChanges, confirmDiscard } from '../hooks/useUnsavedChanges'
import { useConfirm } from '../components/ConfirmProvider'
import { actionError as frontendError, isImageReady } from '../lib/presentation'
import type { Builder, Lab, Room, RoomUpdate, RoomTemplate } from '../types/api'

export default function RoomDetail() {
  const location = useLocation()
  const { roomId } = useParams<{ roomId: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const [roomActionError, setRoomActionError] = useState<string | null>(null)
  const [showEditModal, setShowEditModal] = useState(false)
  const [editRevision, setEditRevision] = useState(0)
  const [creatorTab, setCreatorTab] = useState<'content' | 'lab' | 'publish'>('content')
  const [editData, setEditData] = useState<Partial<RoomUpdate>>({})
  const [labActionError, setLabActionError] = useState<string | null>(null)
  const [labNotice, setLabNotice] = useState<string | null>(null)
  const [builderStopError, setBuilderStopError] = useState<string | null>(null)

  const roomIdNum = roomId ? parseInt(roomId, 10) : 0
  const builderRefreshing = useIsFetching({ queryKey: ['builder', roomIdNum] }) > 0
  const capturePending = useIsMutating({ mutationKey: ['capture-image', roomIdNum] }) > 0

  const { data: liveRoom, isLoading, error, refetch: refetchRoom } = useQuery({
    queryKey: ['room', roomIdNum],
    queryFn: () => getRoom(roomIdNum),
    enabled: !!roomId && roomIdNum > 0,
  })

  const owner = hasCreatorAccess(user) && liveRoom?.creator_id === user?.id
  const workspace = useQuery({ queryKey: ['room-workspace', roomIdNum], queryFn: () => getWorkspace(roomIdNum), enabled: owner,
    refetchIntervalInBackground: false, refetchInterval: query => ['creating', 'pending', 'unknown'].includes(query.state.data?.image_status ?? '') ? 5000 : false })
  const room = owner && workspace.data ? workspace.data.room : liveRoom
  const draft: DraftEditor | undefined = owner && workspace.data ? { data: workspace.data, refresh: workspace.refetch,
    write: async (action, data, revision = workspace.data!.revision) => {
      const result = await writeWorkspace(roomIdNum, revision, action, data)
      queryClient.setQueryData(['room-workspace', roomIdNum], result)
    } } : undefined

  const { builder, isLoading: builderLoading, error: builderError, refetch: refetchBuilder, startBuilderAsync, isStarting, startError, isActive,
    stopBuilderAsync, isStopping: isStoppingBuilder } =
    useBuilder(roomIdNum, !!room && room.creator_id === user?.id && room.room_type !== 'THEORY')

  const canStartLab = hasLearningAccess(user) && !owner && room?.status === 'published' && room.room_type !== 'THEORY'

  const {
    lab,
    isLoading: labLoading,
    isFetching: labRefreshing,
    error: labQueryError,
    refetch: refetchLab,
    refresh: refreshLab,
    startLabAsync,
    isStarting: isStartingLab,
    stopLabAsync,
    isStopping: isStoppingLab,
    extendLabAsync,
    isExtending,
  } = useLab(roomIdNum, canStartLab)

  const updateMutation = useMutation({
    mutationFn: (data: RoomUpdate) => writeWorkspace(roomIdNum, editRevision, 'metadata', data),
    onSuccess: result => {
      queryClient.setQueryData(['room-workspace', roomIdNum], result)
      setShowEditModal(false)
    },
    onError: (err: any) => {
      setRoomActionError(err?.response?.status === 409 ? 'The working copy changed. Refresh the workspace before saving; your open form has not been applied.' : frontendError(err, 'update the room'))
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () => deleteRoom(roomIdNum),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['rooms'] })
      navigate('/')
    },
    onError: (err: any) => {
      setRoomActionError(frontendError(err, 'delete the room'))
    },
  })

  const publishMutation = useMutation({
    mutationFn: () => publishWorkspace(roomIdNum, workspace.data!.revision),
    onSuccess: result => {
      queryClient.setQueryData(['room-workspace', roomIdNum], result)
      void queryClient.invalidateQueries({ queryKey: ['room-templates', roomIdNum] })
      queryClient.invalidateQueries({ queryKey: ['room', roomIdNum] })
      queryClient.invalidateQueries({ queryKey: ['rooms'] })
    },
    onError: (err: any) => {
      setRoomActionError(err?.response?.status === 409 ? 'Publication was not applied. Refresh and review the working copy and image status before trying again. The current published room is unchanged.' : frontendError(err, 'publish the room'))
    },
  })

  const unpublishMutation = useMutation({
    mutationFn: () => unpublishRoom(roomIdNum),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['room-workspace', roomIdNum] })
      queryClient.invalidateQueries({ queryKey: ['room', roomIdNum] })
      queryClient.invalidateQueries({ queryKey: ['rooms'] })
    },
    onError: (err: any) => {
      setRoomActionError(frontendError(err, 'unpublish the room'))
    },
  })

  const handleEdit = () => {
    if (room) {
      setEditData({
        name: room.name,
        description: room.description || '',
        category: room.category,
        categories: room.categories || [room.category],
        difficulty: room.difficulty,
        objectives: room.objectives || '', prerequisites: room.prerequisites || '',
      })
      setEditRevision(workspace.data?.revision ?? 0)
      setShowEditModal(true)
    }
  }

  const handleDelete = async () => {
    if (room && await confirm({ title: 'Delete draft room?', message: `“${room.name}” will be deleted. This cannot be undone.`, label: 'Delete room', danger: true })) {
      setRoomActionError(null)
      deleteMutation.mutate()
    }
  }

  const handlePublication = async (unpublish = false) => {
    if (await confirm({ title: unpublish ? 'Unpublish this room?' : 'Publish this room?', message: unpublish ? 'This room will return to draft and disappear from discovery.' : 'The reviewed working copy becomes the published room. Keep answers and credentials out of the public briefing. Existing machines retain their original image, and earned learner credit is preserved. Updated machine-dependent questions may require learners to stop and start a new machine when ready; machines are never restarted automatically.', label: unpublish ? 'Unpublish room' : 'Publish room', danger: unpublish })) {
      setRoomActionError(null)
      if (unpublish) unpublishMutation.mutate()
      else publishMutation.mutate()
    }
  }

  const handleStartBuilder = async () => {
    try {
      await startBuilderAsync()
    } catch (err: any) {
      setRoomActionError(frontendError(err, 'start the builder'))
    }
  }

  const handleStopBuilder = async () => {
    if (!await confirm({ title: 'Stop and clean up the builder?', message: 'The builder machine and its unsaved disk changes will be destroyed. Save and verify your challenge image first. Captured images are retained.', label: 'Stop builder', danger: true })) return
    setBuilderStopError(null)
    try {
      await stopBuilderAsync()
    } catch {
      setBuilderStopError('Unable to stop the builder. Refresh its status before trying again.')
      queryClient.invalidateQueries({ queryKey: ['builder', roomIdNum] })
    }
  }

  const handleStartLab = async () => {
    setLabActionError(null)
    setLabNotice(null)
    try {
      await startLabAsync()
    } catch (err: any) {
      if (err?.response?.status === 409) {
        refreshLab()
        setLabNotice('This launch conflicts with the current room or lab state. Refreshing status; a usable saved image is required.')
        return
      }
      setLabActionError(frontendError(err, 'start the lab'))
    }
  }

  const handleStopLab = async () => {
    if (!await confirm({ title: 'Stop your lab?', message: 'VPN access will be revoked and this lab’s machine and disk changes will be deleted. The saved room image is retained.', label: 'Stop lab', danger: true })) return
    setLabActionError(null)
    setLabNotice(null)
    try {
      await stopLabAsync()
    } catch (err: any) {
      if (err?.response?.status === 409) {
        refreshLab()
        setLabNotice('Lab state changed. Refreshing status before another Stop attempt.')
        return
      }
      setLabActionError(frontendError(err, 'stop the lab'))
    }
  }

  if (isLoading) {
    return (
      <div className="workspace">
        <div className="inline-loading">
          <span className="spinner" aria-hidden="true"></span>
          Loading room...
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="workspace">
        <div className="empty-state panel">
          <h2 className="empty-state-title">Unable to open this room</h2>
          <p className="empty-state-description">
            {frontendError(error, 'load the room')}
          </p>
          <Link to="/" className="btn btn-primary">
            Back to rooms
          </Link>
          <button type="button" className="btn btn-secondary" onClick={() => void refetchRoom()}>Try again</button>
        </div>
      </div>
    )
  }

  if (!room) {
    return (
      <div className="workspace">
        <div className="inline-loading">Loading...</div>
      </div>
    )
  }

  const pathContext = typeof window !== 'undefined' ? new URLSearchParams(window.location?.search || '').get('path') : null
  const canEdit = owner && !!draft
  const canDelete = hasCreatorAccess(user) && room.creator_id === user?.id && room.status === 'draft'
  const theory = room.room_type === 'THEORY'
  const learnerView = !owner && hasLearningAccess(user) && room.status === 'published'
  const canManageBuilder = !theory && owner && !!draft
  const canInspectBuilder = !theory && hasCreatorAccess(user) && room.creator_id === user?.id
  const canManageTestLab = !theory && hasCreatorAccess(user) && room.creator_id === user?.id
  const canPublish = owner && !!draft && !capturePending && !workspace.isFetching && (theory || draft.data.image_status === 'available') && (room.status !== 'published' || draft.data.dirty)
  const canUnpublish =
    hasCreatorAccess(user) && room.creator_id === user?.id && room.status === 'published'

  const roomContent = <>
      <section className="panel lab-brief" id="room-briefing">
        <div className="briefing-heading"><h2 className="panel-title">Room briefing</h2></div>
        <p className="lab-description">{room.description || 'No description provided.'}</p>
        {room.objectives && <div className="education-copy"><h3>Learning objectives</h3><p>{room.objectives}</p></div>}{room.prerequisites && <div className="education-copy"><h3>Prerequisites</h3><p>{room.prerequisites}</p></div>}
        {theory && !owner && <p className="form-hint">Theory room · no machine required.</p>}
      </section>

      <div className={learnerView ? 'learner-learning-content' : 'creator-learning-workspace'}>


      {(!owner || draft) && <ChallengeQuestions key={`${room.id}:${user?.id}`} room={room} draft={draft} />}
      </div>

  </>
  const roomEnvironment = <>
      <div className={owner ? "creator-environment-grid" : canStartLab ? "room-metadata" : "lab-layout"}>
        <div className="lab-workspace">
          {!theory && draft && <NetworkPorts draft={draft} />}
          {canInspectBuilder && (
            <BuilderSection
              builder={builder}
              isLoading={builderLoading}
              isRefreshing={builderRefreshing}
              isStarting={isStarting}
              startError={startError}
              onStartBuilder={handleStartBuilder}
              isActive={isActive}
              canStart={canManageBuilder}
              isStopping={isStoppingBuilder}
              onStopBuilder={handleStopBuilder}
              stopError={builderStopError}
              queryError={builderError}
              onRefresh={() => void refetchBuilder()}
            />
          )}

          {canManageTestLab && (
            <CreatorChallengeSection
              key={roomIdNum}
              roomId={roomIdNum}
              creatorId={user!.id}
              canCapture={canManageBuilder && builder?.status === 'running'}
              draft={draft}
            />
          )}



          {!theory && !canManageBuilder && !canStartLab && !canManageTestLab && (
            <section className="panel">
              <h2 className="panel-title">Lab environment</h2>
              <p className="muted">
                {hasLearningAccess(user)
                  ? 'Lab environments for this room are managed by the room creator. Instance details appear here when an environment is available.'
                  : 'Builder controls are available to the room creator while the room is in draft.'}
              </p>
            </section>
          )}
        </div>

        {!owner && <aside className="lab-aside">
          <section className="panel">
            <h2 className="panel-title">Room details</h2>
            <dl className="spec-list">
              <div className="spec-row">
                <dt>Category</dt>
                <dd>{(room.categories?.length ? room.categories : [room.category]).map(categoryLabel).join(', ')}</dd>
              </div>
              <div className="spec-row">
                <dt>Difficulty</dt>
                <dd className="spec-gap">
                  <span className={`diff-tag diff-${room.difficulty}`}>{room.difficulty}</span>
                </dd>
              </div>
              <div className="spec-row">
                <dt>Status</dt>
                <dd className="spec-gap">
                  <span className={`badge badge-${room.status}`}>{room.status.replace('_', ' ')}</span>
                </dd>
              </div>
              <div className="spec-row">
                <dt>Created</dt>
                <dd>{formatDateTime(room.created_at)}</dd>
              </div>
              <div className="spec-row">
                <dt>Updated</dt>
                <dd>{formatDateTime(room.updated_at)}</dd>
              </div>
            </dl>
          </section>

        </aside>}
      </div>

  </>

  return (
    <WorkspaceShell className={`lab-page room-experience ${learnerView ? 'learner-room' : 'creator-room'}${owner ? ' room-manager' : ''}`}>
      
      <nav className="crumb" aria-label="Breadcrumb">
        <Link to={discoveryReturn(location.state?.discovery, '/rooms')}>Rooms</Link>
        <span className="crumb-sep">/</span>
        <span>{room.name}</span>
      </nav>

      {pathContext && /^[1-9][0-9]{0,9}$/.test(pathContext) && Number(pathContext) <= 2147483647 && <Link className="auth-return" to={`/paths/${pathContext}`}>← Back to learning path</Link>}
      <header className="lab-header">
        <div className="lab-header-copy">
          <div className="lab-tags">
            <span className={`diff-tag diff-${room.difficulty}`}>{room.difficulty}</span>
            <span className={`badge badge-${room.status}`}>{room.status.replace('_', ' ')}</span>
            {(room.categories?.length ? room.categories : [room.category]).slice(0, owner ? 1 : undefined).map(category => <span className="meta-pill" key={category}>{categoryLabel(category)}</span>)}<span className="meta-pill">{theory ? 'Theory Room' : 'Practical Lab'}</span>
          </div>
          <h1 className="page-heading">{room.name}</h1>
        </div>
        <div className="lab-header-actions">
          {!owner && room.status === 'published' && <BookmarkButton kind="rooms" id={room.id} title={room.name} />}
          {canEdit && (
            <button type="button" onClick={handleEdit} className="btn btn-secondary" disabled={updateMutation.isPending}>
              Edit room
            </button>
          )}
          {owner && creatorTab !== 'publish' && (
            <button
              type="button"
              onClick={() => void handlePublication()}
              className="btn btn-secondary"
              disabled={publishMutation.isPending || !canPublish}
            >
              {publishMutation.isPending ? 'Publishing…' : room.status === 'published' ? 'Publish update' : 'Publish room'}
            </button>
          )}
          {canUnpublish && creatorTab !== 'publish' && <button type="button" className="btn btn-ghost btn-sm" onClick={() => void handlePublication(true)} disabled={unpublishMutation.isPending}>Unpublish room</button>}
          {!owner && canDelete && (
            <button type="button" onClick={handleDelete} className="btn btn-danger" disabled={deleteMutation.isPending}>
              Delete
            </button>
          )}
        </div>
      </header>

      {roomActionError && <div className="error" role="alert">{roomActionError}</div>}
      {owner && workspace.error && <div className="error" role="alert">{frontendError(workspace.error, 'load the working copy')}<button className="btn btn-secondary btn-sm" onClick={() => void workspace.refetch()}>Refresh workspace</button></div>}
      {owner && workspace.isLoading && <p role="status">Loading your working copy…</p>}
      {!owner && room.status === 'published' && <RoomLearningProgress room={room} />}
      {canStartLab && (
        <LabSection
          lab={lab}
          isLoading={labLoading}
          isRefreshing={labRefreshing}
          queryError={labQueryError}
          actionError={labActionError}
          notice={labNotice}
          isStarting={isStartingLab}
          isStopping={isStoppingLab}
          onStartLab={handleStartLab}
          onStopLab={handleStopLab}
          onRetryQuery={() => refetchLab()}
          onExtendLab={extendLabAsync}
          isExtending={isExtending}
        />
      )}
      {owner ? <>
        <div className="room-manager-tabs" role="tablist" aria-label="Room management" onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
          const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'))
          const index = tabs.indexOf(event.target as HTMLButtonElement)
          event.preventDefault()
          const target = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowLeft' ? -1 : 1) + tabs.length) % tabs.length
          tabs[target].focus(); tabs[target].click()
        }}>
          {([['content', 'Content'], ['lab', 'Lab environment'], ['publish', 'Publish']] as const).map(([key, label]) => <button type="button" role="tab" key={key} id={`room-tab-${key}`} aria-selected={creatorTab === key} aria-controls={`room-panel-${key}`} tabIndex={creatorTab === key ? 0 : -1} onClick={() => setCreatorTab(key)}>{label}{key === 'lab' && builder?.status === 'running' && <span className="meta-muted">Builder running</span>}</button>)}
        </div>
        <section role="tabpanel" id="room-panel-content" aria-labelledby="room-tab-content" tabIndex={0} hidden={creatorTab !== 'content'}>{roomContent}</section>
        <section role="tabpanel" id="room-panel-lab" aria-labelledby="room-tab-lab" tabIndex={0} hidden={creatorTab !== 'lab'}>
          <h2 className="room-section-heading">Lab environment</h2>
          {theory ? <p className="panel muted">Theory rooms use browser lessons and questions; no lab environment is needed.</p> : roomEnvironment}
        </section>
        <section role="tabpanel" id="room-panel-publish" aria-labelledby="room-tab-publish" tabIndex={0} hidden={creatorTab !== 'publish'}>
          <section className="panel room-publish-panel"><div className="briefing-heading"><h2 className="panel-title">Publish</h2><span className={`badge badge-${room.status}`}>{room.status}</span></div>
            <p>{draft?.data.dirty ? 'Unpublished changes are ready for review.' : room.status === 'published' ? 'No unpublished changes.' : 'Review your room before publishing.'}</p>
            <dl className="publication-readiness"><div><dt>Content</dt><dd>{draft ? `${draft.data.tasks.length} tasks · ${draft.data.questions.length} questions` : 'Loading…'}</dd></div><div><dt>Image</dt><dd>{theory ? 'Not required' : draft?.data.image_status === 'available' ? 'Ready' : draft?.data.image_status || 'No saved image'}</dd></div><div><dt>Readiness</dt><dd>{capturePending ? 'Image capture in progress' : !draft ? 'Working copy unavailable' : !theory && draft.data.image_status !== 'available' ? 'Save a ready challenge image' : canPublish ? 'Ready to publish' : room.status === 'published' ? 'Published version is current' : 'Review required'}</dd></div></dl>
            <div className="button-row"><button type="button" className="btn btn-primary" disabled={!canPublish || publishMutation.isPending} onClick={() => void handlePublication()}>{publishMutation.isPending ? 'Publishing…' : room.status === 'published' ? 'Publish update' : 'Publish room'}</button>{canUnpublish && <button type="button" className="btn btn-secondary" disabled={unpublishMutation.isPending} onClick={() => void handlePublication(true)}>Unpublish room</button>}{canDelete && <button type="button" className="btn btn-ghost" disabled={deleteMutation.isPending} onClick={() => void handleDelete()}>Delete draft room</button>}</div>
            <details className="context-help"><summary>Publication &amp; learner credit</summary><p>Only publishing makes draft changes visible. Earned credit is retained; changed answers or points create a new grading version. Existing machines keep their saved image.</p></details>
            <details className="context-help"><summary>Room history</summary><p>Created {formatDateTime(room.created_at)} · Updated {formatDateTime(room.updated_at)}</p></details>
            {room.status === 'published' && <div className="room-bookmark"><span className="muted">Keep this room in Saved Content</span><BookmarkButton kind="rooms" id={room.id} title={room.name} /></div>}
          </section>
        </section>
      </> : <>{roomContent}</>}

      {showEditModal && (
        <EditRoomModal
          room={room}
          initialData={editData}
          onClose={() => setShowEditModal(false)}
          onSave={(data) => updateMutation.mutate(data)}
          isPending={updateMutation.isPending}
          errorMessage={updateMutation.error ? frontendError(updateMutation.error, "save room changes") : null}
        />
      )}
    </WorkspaceShell>
  )
}

function BuilderSection({
  builder,
  isLoading,
  isStarting,
  startError,
  onStartBuilder,
  isActive,
  canStart,
  isStopping,
  onStopBuilder,
  stopError,
  isRefreshing,
  queryError,
  onRefresh,
}: {
  builder: Builder | null | undefined
  isLoading: boolean
  isStarting: boolean
  startError: any
  onStartBuilder: () => void
  isActive: boolean
  canStart: boolean
  isStopping: boolean
  onStopBuilder: () => void
  stopError: string | null
  isRefreshing: boolean
  queryError: unknown
  onRefresh: () => void
}) {
  const getStatusLabel = (status: string) => {
    const labels: Record<string, string> = {
      pending: 'Pending',
      provisioning: 'Provisioning',
      running: 'Running',
      stopping: 'Stopping',
      stopped: 'Stopped',
      failed: 'Failed',
    }
    return labels[status] || status
  }

  const { user } = useAuth()
  const profileFilename = vpnProfileFilename(user?.username ?? 'user')
  const [isDownloadingVpn, setIsDownloadingVpn] = useState(false)
  const [vpnDownloadError, setVpnDownloadError] = useState<string | null>(null)

  const handleDownloadVpnProfile = async () => {
    setVpnDownloadError(null)
    setIsDownloadingVpn(true)
    let objectUrl: string | null = null
    try {
      const { blob, contentDisposition } = await downloadVpnProfile()
      objectUrl = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = objectUrl
      anchor.download = safeProfileFilename(contentDisposition, user?.username ?? 'user')
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
    } catch (err: any) {
      setVpnDownloadError(vpnProfileErrorMessage(err))
    } finally {
      if (objectUrl !== null) {
        URL.revokeObjectURL(objectUrl)
      }
      setIsDownloadingVpn(false)
    }
  }

  const startButton = (label: string, pendingLabel: string) => canStart && (
    <button type="button" onClick={onStartBuilder} className="btn btn-primary" disabled={isStarting || isStopping || !!queryError}>
      {isStarting ? (
        <>
          <span className="spinner" aria-hidden="true"></span>
          {pendingLabel}
        </>
      ) : (
        label
      )}
    </button>
  )

  if (isLoading) {
    return (
      <section className="panel builder-panel" id="builder-workspace">
        <header className="builder-head">
          <h2 className="panel-title">Private builder</h2>
        </header>
        <div className="inline-loading">
          <span className="spinner" aria-hidden="true"></span>
          Loading builder status...
        </div>
      </section>
    )
  }

  if (queryError && !builder) return <section className="panel builder-panel" id="builder-workspace"><h2 className="panel-title">Private builder</h2><div className="error" role="alert">{frontendError(queryError, 'load builder status')}</div><button type="button" className="btn btn-secondary" onClick={onRefresh} disabled={isRefreshing}>{isRefreshing ? 'Refreshing…' : 'Refresh builder status'}</button></section>

  if (!builder) {
    return (
      <section className="panel builder-panel" id="builder-workspace">
        <header className="builder-head">
          <h2 className="panel-title">Private builder</h2>
          <span className="badge badge-stopped">Not Started</span>
        </header>
        {startButton('Start builder', 'Starting...')}
        {startError && (
          <div className="error" role="alert">
            {frontendError(startError, 'start the builder')}
          </div>
        )}
      </section>
    )
  }

  return (
    <section className={`panel builder-panel builder-${builder.status}`} id="builder-workspace">
      <header className="builder-head">
        <h2 className="panel-title">Private builder</h2>
        <span className={`badge badge-${builder.status}`}>{getStatusLabel(builder.status)}</span>
      </header>

      <button type="button" className="btn btn-ghost btn-sm status-refresh" onClick={onRefresh} disabled={isRefreshing}>{isRefreshing ? <span className="spinner" aria-hidden="true" /> : <Icon name="refresh" size={16} />} {isRefreshing ? 'Refreshing…' : 'Refresh builder status'}</button>
      {queryError && <div className="error" role="alert">{frontendError(queryError, "refresh builder status")}</div>}
      {['pending', 'provisioning', 'stopping', 'failed'].includes(builder.status) && <EnvironmentStatus status={builder.status} />}
      {stopError && <div className="error" role="alert">{stopError}</div>}
      {['pending', 'provisioning', 'running', 'stopping', 'failed'].includes(builder.status) && (
        <div className="builder-status-row">
          <button type="button" className="btn btn-danger" onClick={onStopBuilder}
            disabled={isStopping || isStarting || builder.status === 'stopping'}>
            {isStopping || builder.status === 'stopping' ? 'Stopping...' : 'Stop builder'}
          </button>
        </div>
      )}

      {startError && !isActive && (
        <div className="error" role="alert">
          {frontendError(startError, 'start the builder')}
        </div>
      )}

      {builder.status === 'running' && (
        <>
          {builder.private_ip && <ConnectionGuide address={builder.private_ip} profileFilename={profileFilename} builder sshUsername={builder.ssh_username} download={<>
            {vpnDownloadError && <div className="error" role="alert">{vpnDownloadError}</div>}
            <button type="button" onClick={handleDownloadVpnProfile} className="btn btn-secondary btn-sm" disabled={isDownloadingVpn}>
              {isDownloadingVpn ? <span className="spinner" aria-hidden="true" /> : <Icon name="download" size={16} />}
              {isDownloadingVpn ? 'Downloading...' : 'Download VPN Profile'}
            </button>
          </>} />}
          <details className="context-help"><summary>Training account SSH settings</summary><BuilderSshAuth key={builder.id} builder={builder} onSaved={onRefresh} /></details>
          <dl className="spec-list builder-specs">
            <div className="spec-row">
              <dt>Expires</dt>
              <dd className="mono">{formatDateTime(builder.expires_at)}</dd>
            </div>
          </dl>

        </>
      )}

      {builder.status === 'failed' && (
        <div className="builder-failed">
          {builder.error_message && (
            <div className="error" role="alert">
              Builder operation failed. Resources may still exist. Refresh status and review before starting another builder or requesting cleanup.
            </div>
          )}
          {startButton('Retry start builder', 'Retrying...')}
        </div>
      )}

      {builder.status === 'stopped' && (
        <div className="builder-stopped">
          
          {startButton('Start builder', 'Starting...')}
        </div>
      )}

      {!isActive &&
        builder.status !== 'running' &&
        builder.status !== 'failed' &&
        builder.status !== 'stopped' && (
          <p className="muted">Unknown builder state: {builder.status}</p>
        )}
    </section>
  )
}

function testLabErrorMessage(error: any, action: string): string {
  // Use fixed messages: infrastructure/database errors may contain private details.
  const status = error?.response?.status
  if (status === 503) return 'Test lab service is temporarily unavailable. Refresh status and try again.'
  if (status === 403) return 'You do not have permission to manage this test lab.'
  if (status === 404) return 'The room or captured image is no longer available. Refresh and try again.'
  if (status === 409) return action === 'stop'
    ? 'The lab state changed. Refresh status before trying Stop again.'
    : 'Another lab is active or the image is still being resolved. Refresh lab and image status.'
  return `Unable to ${action}. Refresh status before trying again.`
}

function CreatorChallengeSection({ roomId, creatorId, canCapture, draft }: {
  roomId: number
  creatorId: number
  canCapture: boolean
  draft?: DraftEditor
}) {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const testLab = useCreatorTestLab(roomId, creatorId)
  const [actionError, setActionError] = useState<string | null>(null)
  const [templateNotice, setTemplateNotice] = useState<string | null>(null)
  const captureKeyRef = useRef<string | null>(null)
  const actionInFlightRef = useRef(false)
  const templatesQuery = useQuery({
    queryKey: ['room-templates', roomId],
    queryFn: () => listRoomTemplates(roomId),
    refetchIntervalInBackground: false,
    refetchInterval: (query) => query.state.data?.some(template =>
      template.machines.some(machine => ['pending', 'creating', 'unknown'].includes(machine.status)),
    ) ? 5000 : false,
  })

  const saveTemplateMutation = useMutation({
    mutationKey: ['capture-image', roomId],
    mutationFn: () => {
      const idempotencyKey = captureKeyRef.current ?? newIdempotencyKey()
      captureKeyRef.current = idempotencyKey
      return saveRoomTemplate(roomId, idempotencyKey)
    },
    retry: (failureCount, error) => shouldReuseCaptureKey(error) && failureCount < 2,
    retryDelay: attempt => Math.min(1000 * 2 ** attempt, 5000),
    onSuccess: template => {
      captureKeyRef.current = null
      setActionError(null)
      const machine = template.machines[0]
      if (machine?.status === 'failed') {
        setTemplateNotice(null)
        setActionError('Image capture failed. The current published image is unchanged. Review the builder, then retry capture.')
      } else {
        setTemplateNotice(machine?.status === 'available'
          ? 'Image captured and ready for review.'
          : 'Capture accepted. Image status refreshes automatically.')
      }
      // Cache the accepted version immediately, including when the subsequent GET fails.
      queryClient.setQueryData<RoomTemplate[]>(['room-templates', roomId], current =>
        [template, ...(current ?? []).filter(row => row.id !== template.id)].sort((a, b) => b.version - a.version))
      return Promise.all([queryClient.invalidateQueries({ queryKey: ['room-workspace', roomId] }), queryClient.invalidateQueries({ queryKey: ['room-templates', roomId] })])
    },
    onError: error => {
      if (!shouldRetainCaptureKey(error)) captureKeyRef.current = null
      setTemplateNotice(null)
      setActionError(testLabErrorMessage(error, 'save the challenge image'))
    },
  })

  const refresh = async () => {
    const [labResult, versionsResult] = await Promise.all([testLab.refetch(), templatesQuery.refetch(), draft?.refresh()])
    if (labResult.error || versionsResult.error) {
      setActionError(testLabErrorMessage(labResult.error || versionsResult.error, 'refresh status'))
    }
  }
  const launch = async (template: RoomTemplate) => {
    if (actionInFlightRef.current) return
    actionInFlightRef.current = true
    setActionError(null)
    setTemplateNotice(null)
    try {
      await testLab.startLabAsync(template.id)
    } catch (error) {
      setActionError(testLabErrorMessage(error, 'launch the test lab'))
      // Refresh even after a transport failure: the server may have accepted launch.
      await refresh()
    } finally {
      actionInFlightRef.current = false
    }
  }
  const stop = async () => {
    if (!await confirm({ title: 'Stop the test lab?', message: 'The test machine and its disk changes will be deleted. Your builder and saved versions will be preserved.', label: 'Stop test lab', danger: true })) return
    if (actionInFlightRef.current) return
    actionInFlightRef.current = true
    setActionError(null)
    try {
      await testLab.stopLabAsync()
    } catch (error) {
      setActionError(testLabErrorMessage(error, 'stop'))
      await refresh()
    } finally {
      actionInFlightRef.current = false
    }
  }
  const activeLab = !!testLab.lab && ['pending', 'provisioning', 'running', 'stopping'].includes(testLab.lab.status)
  const launchDisabled = activeLab || testLab.isLoading || testLab.isFetching || !!testLab.error
    || testLab.isStarting || testLab.isStopping || !!templatesQuery.error || templatesQuery.isFetching

  return (
    <>
      <section className="panel template-block" id="saved-versions">
        <div className="briefing-heading"><h2 className="panel-title">Challenge image</h2>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setActionError(null); void refresh() }}
          disabled={testLab.isFetching || templatesQuery.isFetching}>
          {testLab.isFetching || templatesQuery.isFetching ? 'Refreshing...' : 'Refresh image status'}
        </button>
        </div>
        {canCapture && (
          <button type="button" className="btn btn-primary" disabled={saveTemplateMutation.isPending || ['pending', 'creating', 'unknown'].includes(draft?.data.image_status ?? '')}
            onClick={async () => { if (await confirm({ title: 'Capture a challenge image?', message: 'A new saved version will be created from the running builder. Enable challenge services on boot before capturing. The builder is retained.', label: 'Save image' })) { setActionError(null); setTemplateNotice(null); saveTemplateMutation.mutate() } }}>
            {saveTemplateMutation.isPending ? 'Capturing image…' : 'Save challenge image'}
          </button>
        )}
        {actionError && <div className="error" role="alert">{actionError}</div>}
        {templateNotice && <p className="muted">{templateNotice}</p>}
        {templatesQuery.isLoading && <p className="inline-loading">Loading image status…</p>}
        {templatesQuery.error && <div className="error" role="alert">
          {testLabErrorMessage(templatesQuery.error, 'load image status')}
        </div>}
        {!templatesQuery.isLoading && !templatesQuery.error && !templatesQuery.data?.length && <p>No saved image.</p>}
        <ul className="template-list">
          {(templatesQuery.data ?? []).filter(template => template.id === draft?.data.image_template_id || template.is_published).map(template => {
            const ready = isImageReady(template.machines)
            const status = ready ? 'available' : template.machines[0]?.status || 'unavailable'
            return (
              <li key={template.id} className="template-item">
                <div className="version-heading"><Icon name="layers" size={20} /><div><strong>{template.is_published ? 'Current published image' : 'Unpublished captured image'}</strong><span className="meta-muted">Saved {formatDateTime(template.created_at)}</span>
                  {template.machines[0]?.error_message && <p className="error" role="alert">Capture details: {template.machines[0].error_message}</p>}
                </div></div>
                <span className={`badge badge-${ready ? 'running' : status === 'available' ? 'failed' : status}`}>{ready ? 'Ready' : status === 'available' ? 'Unavailable image' : status}</span>
                <button type="button" className="btn btn-secondary" disabled={!ready || launchDisabled}
                  onClick={() => void launch(template)}>
                  {testLab.isStarting ? 'Launching...' : 'Test captured image'}
                </button>
              </li>
            )
          })}
        </ul>
        {activeLab && <p className="muted">Stop the current test lab before launching another image.</p>}
      </section>
      <LabSection isTestLab lab={testLab.lab} isLoading={testLab.isLoading} isRefreshing={testLab.isFetching || templatesQuery.isFetching} queryError={testLab.error}
        actionError={null} notice={null} isStarting={testLab.isStarting} isStopping={testLab.isStopping}
        onStartLab={() => undefined} onStopLab={() => void stop()} onRetryQuery={() => void refresh()} />
    </>
  )
}

function vpnProfileErrorMessage(err: any): string {
  const status = err?.response?.status
  if (status === 503) return 'VPN service is currently unavailable.'
  if (status === 502) return 'VPN gateway error. Please try again.'
  if (status === 403) return 'VPN access is disabled for this account.'
  return 'Unable to download the VPN profile. Please try again.'
}

function LabSection({
  isTestLab = false,
  lab,
  isLoading,
  isRefreshing,
  queryError,
  actionError,
  notice,
  isStarting,
  isStopping,
  onStartLab,
  onStopLab,
  onRetryQuery,
  onExtendLab,
  isExtending,
}: {
  isTestLab?: boolean
  lab: Lab | null | undefined
  isLoading: boolean
  isRefreshing: boolean
  queryError: any
  actionError: string | null
  notice: string | null
  isStarting: boolean
  isStopping: boolean
  onStartLab: () => void
  onStopLab: () => void
  onRetryQuery: () => void
  onExtendLab?: () => Promise<Lab>
  isExtending?: boolean
}) {
  const title = isTestLab ? 'Creator test lab' : 'Lab machine'
  const statusLabels: Record<string, string> = {
    pending: 'Starting',
    provisioning: 'Starting',
    running: 'Running',
    stopping: 'Stopping',
    stopped: 'Stopped',
    failed: 'Failed',
  }

  const { user } = useAuth()
  const remaining = useLabClock(isTestLab ? null : lab)
  const displayStatus = !isTestLab && lab && (lab.expired_at || remaining === 0) && ['pending', 'provisioning', 'running'].includes(lab.status) ? 'expired' : lab?.status
  const profileFilename = vpnProfileFilename(user?.username ?? 'user')
  const [isDownloadingVpn, setIsDownloadingVpn] = useState(false)
  const [vpnDownloadError, setVpnDownloadError] = useState<string | null>(null)

  const handleDownloadVpnProfile = async () => {
    setVpnDownloadError(null)
    setIsDownloadingVpn(true)
    let objectUrl: string | null = null
    try {
      const { blob, contentDisposition } = await downloadVpnProfile()
      objectUrl = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = objectUrl
      anchor.download = safeProfileFilename(contentDisposition, user?.username ?? 'user')
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
    } catch (err: any) {
      setVpnDownloadError(vpnProfileErrorMessage(err))
    } finally {
      if (objectUrl !== null) {
        URL.revokeObjectURL(objectUrl)
      }
      setIsDownloadingVpn(false)
    }
  }

  const startButton = (label: string, pendingLabel: string) => (
    <button type="button" onClick={onStartLab} className="btn btn-primary" disabled={isStarting || isStopping || !!queryError}>
      {isStarting ? (
        <>
          <span className="spinner" aria-hidden="true"></span>
          {pendingLabel}
        </>
      ) : (
        label
      )}
    </button>
  )

  const stopButton = (label: string, pendingLabel: string) => (
    <button
      type="button"
      onClick={onStopLab}
      className="btn btn-secondary"
      disabled={isStopping || lab?.status === 'stopping'}
    >
      {isStopping ? (
        <>
          <span className="spinner" aria-hidden="true"></span>
          {pendingLabel}
        </>
      ) : (
        label
      )}
    </button>
  )

  const messages = (
    <>
      {actionError && (
        <div className="error" role="alert">
          {actionError}
        </div>
      )}
      {notice && <p className="muted">{notice}</p>}
    </>
  )

  if (!isTestLab) {
    const active = lab?.status === 'running' && displayStatus !== 'expired'
    return <section className="panel learner-machine" id="learner-workspace" aria-labelledby="learner-machine-title">
      <div className="learner-machine-bar"><h2 className="panel-title" id="learner-machine-title">Lab machine</h2>
        {isLoading && !lab ? <span role="status">Loading lab status…</span> : <span className={`badge badge-${displayStatus || 'stopped'}`}>{displayStatus === 'expired' ? 'Expired' : statusLabels[lab?.status || 'stopped'] || lab?.status}</span>}
        {lab && ['running', 'pending', 'provisioning', 'stopping'].includes(lab.status) && stopButton('Stop Machine', 'Stopping…')}
        {(!lab || lab.status === 'stopped') && !isLoading && startButton('Start Machine', 'Starting…')}
        <button type="button" className="btn btn-ghost btn-sm machine-refresh" aria-label="Refresh lab status" onClick={onRetryQuery} disabled={isRefreshing}>{isRefreshing ? <span className="spinner" aria-hidden="true" /> : <Icon name="refresh" size={16} />}</button>
      </div>
      {messages}
      {queryError && <div className="error" role="alert">{frontendError(queryError, 'refresh lab status')} <button className="btn btn-secondary btn-sm" onClick={onRetryQuery}>Try again</button></div>}
      {lab && ['pending', 'provisioning', 'stopping', 'failed'].includes(lab.status) && <EnvironmentStatus status={displayStatus || lab.status} />}
      {lab && (active || displayStatus === 'expired') && <LabTimer key={lab.id} lab={lab} seconds={remaining} onExtend={onExtendLab} pending={isExtending} compact />}
      {active && lab?.private_ip && <ConnectionGuide address={lab.private_ip} profileFilename={profileFilename} compact download={<>
        {vpnDownloadError && <div className="error" role="alert">{vpnDownloadError}</div>}
        <button type="button" onClick={handleDownloadVpnProfile} className="btn btn-secondary btn-sm" disabled={isDownloadingVpn}>{isDownloadingVpn ? 'Downloading…' : 'Download VPN Profile'}</button>
      </>} />}
      {lab?.status === 'failed' && <><p className="error" role="alert">The lab operation failed. Resources may still exist. Refresh status and retry cleanup before launching another lab.</p><div className="button-row">{stopButton('Retry cleanup', 'Stopping…')}{startButton('Retry start lab', 'Retrying…')}</div></>}
      {lab && !['pending','provisioning','running','stopping','failed','stopped'].includes(lab.status) && <p className="error" role="alert">Unknown lab state: {lab.status}. Refresh status before continuing.</p>}
    </section>
  }

  if (isLoading && !lab) {
    return (
      <section className="panel lab-instance-panel" id={isTestLab ? "test-workspace" : "learner-workspace"}>
        <header className="lab-instance-head">
          <h2 className="panel-title">{title}</h2>
        </header>
        <div className="inline-loading">
          <span className="spinner" aria-hidden="true"></span>
          Loading lab status...
        </div>
      </section>
    )
  }

  if (queryError && !lab) {
    return (
      <section className="panel lab-instance-panel" id={isTestLab ? "test-workspace" : "learner-workspace"}>
        <header className="lab-instance-head">
          <h2 className="panel-title">{title}</h2>
        </header>
        <div className="error" role="alert">
          {isTestLab ? testLabErrorMessage(queryError, 'load test lab status') : frontendError(queryError, 'load lab status')}
        </div>
        <div>
          <button type="button" onClick={onRetryQuery} className="btn btn-secondary">
            Try again
          </button>
        </div>
      </section>
    )
  }

  if (!lab) {
    return (
      <section className="panel lab-instance-panel lab-instance-idle" id={isTestLab ? "test-workspace" : "learner-workspace"}>
        <header className="lab-instance-head">
          <h2 className="panel-title">{title}</h2>
          <span className="badge badge-stopped">Not Started</span>
        </header>
        <p className="lab-instance-copy">
          {isTestLab ? 'No test lab running. Test labs expire after 1 hour.' : 'Start a lab instance for this room. It runs for 1 hour and expires automatically.'}
        </p>
        {messages}
        {!isTestLab && startButton('Start Machine', 'Starting...')}
      </section>
    )
  }

  return (
    <section className={`panel lab-instance-panel lab-instance-${lab.status}`} id={isTestLab ? "test-workspace" : "learner-workspace"}>
      <header className="lab-instance-head">
        <h2 className="panel-title">{title}</h2>
        <div className="lab-header-status"><span className={`badge badge-${displayStatus}`}>{displayStatus === 'expired' ? 'Expired' : statusLabels[lab.status] || lab.status}</span>
          {lab.status === 'running' && stopButton('Stop Machine', 'Stopping...')}
        </div>
      </header>

      <button type="button" className="btn btn-ghost btn-sm status-refresh" onClick={onRetryQuery} disabled={isRefreshing}>{isRefreshing ? <span className="spinner" aria-hidden="true" /> : <Icon name="refresh" size={16} />} {isRefreshing ? 'Refreshing…' : 'Refresh lab status'}</button>
      {(!isTestLab || !['running', 'stopped'].includes(lab.status)) && <EnvironmentStatus status={displayStatus || lab.status} />}
      {!isTestLab && <LabTimer key={lab.id} lab={lab} seconds={remaining} onExtend={onExtendLab} pending={isExtending} />}
      {messages}
      {queryError && <div className="error" role="alert">{frontendError(queryError, "refresh lab status")}</div>}

      {(lab.status === 'pending' || lab.status === 'provisioning') && (
        <div className="lab-instance-status">
          <span className="spinner" aria-hidden="true"></span>
          <p>
            {lab.status === 'pending'
              ? 'Lab instance queued. Provisioning will begin shortly.'
              : 'Provisioning lab instance. This may take a few minutes.'}
          </p>
          {stopButton(isTestLab ? 'Stop test lab' : 'Stop lab', 'Stopping...')}
        </div>
      )}

      {lab.status === 'stopping' && (
        <div className="lab-instance-status">
          <span className="spinner" aria-hidden="true"></span>
          <p>Stopping lab instance...</p>
          {stopButton('Stop Machine', 'Stopping...')}
        </div>
      )}

      {lab.status === 'running' && (isTestLab || remaining !== 0) && (
        <>
          {lab.private_ip && <ConnectionGuide address={lab.private_ip} profileFilename={profileFilename} download={<>
            {vpnDownloadError && <div className="error" role="alert">{vpnDownloadError}</div>}
            <button type="button" onClick={handleDownloadVpnProfile} className="btn btn-secondary btn-sm" disabled={isDownloadingVpn}>
              {isDownloadingVpn ? <span className="spinner" aria-hidden="true" /> : <Icon name="download" size={16} />}
              {isDownloadingVpn ? 'Downloading...' : 'Download VPN Profile'}
            </button>
          </>} />}
          <dl className="spec-list lab-instance-specs">
            <div className="spec-row">
              <dt>Expires</dt>
              <dd className="mono">{formatDateTime(lab.expires_at)}</dd>
            </div>
          </dl>
        </>
      )}

      {lab.status === 'failed' && (
        <>
          {(isTestLab || lab.error_message) && (
            <div className="error" role="alert">
              {isTestLab ? 'The test lab operation failed. Refresh status; choose a ready version above to launch again.' : 'The lab operation failed. Resources may still exist. Refresh status and retry cleanup before launching another lab.'}
            </div>
          )}
          <div className="lab-instance-actions">
            {stopButton('Retry cleanup', 'Stopping...')}
            {!isTestLab && startButton('Retry start lab', 'Retrying...')}
          </div>
        </>
      )}

      {lab.status === 'stopped' && (
        <>
          <p className="lab-instance-copy">Your lab instance has been stopped.</p>
          <div className="lab-instance-actions">{!isTestLab && startButton('Start Machine', 'Starting...')}</div>
        </>
      )}

      {!['pending', 'provisioning', 'stopping', 'running', 'failed', 'stopped'].includes(lab.status) && (
        <p className="muted">Unknown lab state: {lab.status}</p>
      )}
    </section>
  )
}

function EditRoomModal({
  room,
  initialData,
  onClose,
  onSave,
  isPending,
  errorMessage,
}: {
  room: Room
  initialData: Partial<RoomUpdate>
  onClose: () => void
  onSave: (data: RoomUpdate) => void
  isPending: boolean
  errorMessage: string | null
}) {
  const [name, setName] = useState(initialData.name || '')
  const [description, setDescription] = useState(initialData.description || '')
  const [selectedCategories, setSelectedCategories] = useState(initialData.categories?.length ? initialData.categories : initialData.category ? [initialData.category] : [])
  const category = selectedCategories[0] || ''
  const [difficulty, setDifficulty] = useState(initialData.difficulty || 'easy')
  const [objectives, setObjectives] = useState(initialData.objectives || ''), [prerequisites, setPrerequisites] = useState(initialData.prerequisites || '')

  const dirty = objectives !== (initialData.objectives || '') || prerequisites !== (initialData.prerequisites || '') || name !== (initialData.name || '') || description !== (initialData.description || '') || JSON.stringify(selectedCategories) !== JSON.stringify(initialData.categories?.length ? initialData.categories : initialData.category ? [initialData.category] : []) || difficulty !== (initialData.difficulty || 'easy')
  useUnsavedChanges(dirty)
  const close = () => { if (confirmDiscard(dirty)) onClose() }
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (selectedCategories.length) onSave({ name, description, category, categories: selectedCategories, difficulty, objectives, prerequisites })
  }

  return (
    <Dialog title="Edit room" onClose={close} busy={isPending}>
        <form onSubmit={handleSubmit}>
          <fieldset className="modal-body modal-fields" disabled={isPending}>
            {errorMessage && (
              <div className="error" role="alert">
                {errorMessage}
              </div>
            )}
            <p className="muted modal-room-name">{room.name}</p>

            <div className="form-group">
              <label htmlFor="edit-name">Name *</label>
              <input
                id="edit-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                maxLength={100}
              />
            </div>

            <div className="form-group">
              <label htmlFor="edit-description">Public briefing</label>
              <textarea
                id="edit-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                maxLength={2000}
                rows={3}
              />
            </div>

            <div className="form-group"><label htmlFor="edit-objectives">Learning objectives</label><textarea id="edit-objectives" maxLength={2000} value={objectives} onChange={e => setObjectives(e.target.value)} /></div>
            <div className="form-group"><label htmlFor="edit-prerequisites">Prerequisites</label><textarea id="edit-prerequisites" maxLength={2000} value={prerequisites} onChange={e => setPrerequisites(e.target.value)} /></div>
            <div className="form-row">
              <div className="form-group">
                <label htmlFor="edit-category">Category *</label>
                <CategoryMultiSelect id="edit-category" values={selectedCategories} onChange={setSelectedCategories} />
              </div>

              <div className="form-group">
                <label htmlFor="edit-difficulty">Difficulty *</label>
                <select
                  id="edit-difficulty"
                  value={difficulty}
                  onChange={(e) => setDifficulty(e.target.value)}
                  required
                >
                  <option value="easy">Easy</option>
                  <option value="medium">Medium</option>
                  <option value="hard">Hard</option>
                </select>
              </div>
            </div>
          </fieldset>
          <footer className="modal-footer">
            <button type="button" onClick={close} className="btn btn-secondary" disabled={isPending}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={isPending}>
              {isPending ? 'Saving...' : 'Save changes'}
            </button>
          </footer>
        </form>
    </Dialog>
  )
}
