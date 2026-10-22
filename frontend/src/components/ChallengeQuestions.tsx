import { hasCreatorAccess, hasLearningAccess } from '../lib/permissions'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { createQuestion, deleteQuestion, getProgress, listQuestions, orderQuestions, submitAnswer, updateQuestion, listTasks, orderTasks, deleteTask, getEditableAnswer, getAcceptedAnswers } from '../api/challenges'
import TaskEditor from './TaskEditor'
import SafeMarkdown from './SafeMarkdown'
import { useUnsavedChanges, confirmDiscard } from '../hooks/useUnsavedChanges'
import type { Question, QuestionWrite, Room, RoomTask, AcceptedAnswer } from '../types/api'
import { actionError } from '../lib/presentation'
import Dialog from './Dialog'
import Icon from './Icon'
import ContextHelp from './ContextHelp'
import { useConfirm } from './ConfirmProvider'
import type { DraftEditor } from '../api/workspace'

function learningError(error: unknown, action: string): string {
  if ((error as { response?: { status?: number } })?.response?.status === 409) {
    return 'The room changed. Refresh the workspace before retrying. Your open form has not been applied.'
  }
  return actionError(error, action)
}

export default function ChallengeQuestions({ room, draft }: { room: Room; draft?: DraftEditor }) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const author = hasCreatorAccess(user) && user?.id === room.creator_id
  const learner = hasLearningAccess(user) && !author && room.status === 'published'
  const questionKey = ['questions', room.id, user?.id]
  const progressKey = ['progress', room.id, user?.id]
  const questions = useQuery({ queryKey: questionKey, queryFn: () => listQuestions(room.id), enabled: !draft && !!user && (author || learner) })
  const progress = useQuery({ queryKey: progressKey, queryFn: () => getProgress(room.id), enabled: learner })
  const tasks = useQuery({ queryKey: ['room-tasks', room.id, user?.id], queryFn: () => listTasks(room.id), enabled: !draft && !!user && (author || learner), retry: false })
  const [taskEditor, setTaskEditor] = useState<RoomTask | 'new' | null>(null)
  const [editor, setEditor] = useState<Question | 'new' | null>(null)
  const [preview, setPreview] = useState(false)
  const [busy, setBusy] = useState(false)
  const operation = useRef(false)
  const [error, setError] = useState('')
  const rows = draft?.data.questions ?? questions.data?.questions ?? []
  const taskRows = draft?.data.tasks ?? tasks.data?.tasks ?? []
  const taskIds = new Set(taskRows.map(t => t.id))
  const ungrouped = rows.filter(q => !q.task_id || !taskIds.has(q.task_id))
  const groups = [...taskRows.map(task => ({ key: String(task.id), task, title: task.title, questions: rows.filter(q => q.task_id === task.id) })), ...(!taskRows.length || ungrouped.length > 0 ? [{ key: 'ungrouped', task: null, title: taskRows.length ? 'Ungrouped questions' : 'Challenge questions', questions: ungrouped }] : [])]
  const [openTask, setOpenTask] = useState<string | null | undefined>(() => {
    if (!learner || typeof window === 'undefined') return undefined
    try { const saved = localStorage.getItem(`secforge:room-task:${user?.id}:${room.id}`); return saved === null ? undefined : saved || null } catch { return undefined }
  })
  const activeTask = openTask === null ? null : groups.some(g => g.key === openTask) ? openTask : groups[0]?.key
  const chooseTask = (key: string | null, scroll = false) => {
    setOpenTask(key)
    try { localStorage.setItem(`secforge:room-task:${user?.id}:${room.id}`, key ?? '') } catch { /* Storage is optional. */ }
    if (scroll && key) requestAnimationFrame(() => {
      const heading = document.querySelector<HTMLElement>(`#room-task-${key} > summary`)
      heading?.focus({ preventScroll: true }); heading?.scrollIntoView({ block: 'start' })
    })
  }
  const [accepted, setAccepted] = useState<{ key: string; rows: AcceptedAnswer[]; status: 'loading' | 'ready' | 'error' }>({ key: '', rows: [], status: 'loading' })
  const [answerReload, setAnswerReload] = useState(0)
  const acceptedKey = `${user?.id}:${room.id}`
  useEffect(() => {
    if (!learner) return
    const controller = new AbortController()
    setAccepted(current => ({ key: acceptedKey, rows: current.key === acceptedKey ? current.rows : [], status: 'loading' }))
    void getAcceptedAnswers(room.id, controller.signal).then(rows => {
      if (!controller.signal.aborted) setAccepted({ key: acceptedKey, rows, status: 'ready' })
    }).catch(() => { if (!controller.signal.aborted) setAccepted(current => ({ ...current, status: 'error' })) })
    return () => controller.abort()
  }, [learner, room.id, acceptedKey, progress.dataUpdatedAt, answerReload])
  const locked = draft ? false : questions.data?.authoring_locked ?? false
  const reload = async () => { if (draft) await draft.refresh(); else await Promise.all([tasks.refetch(), questions.refetch()]) }
  if (!author && !learner) return null
  const reorder = async (first: number, second: number) => {
    const index = rows.findIndex(q => q.id === first), target = rows.findIndex(q => q.id === second)
    if (index < 0 || target < 0) return
    if (operation.current) return
    operation.current = true; setBusy(true); setError('')
    const ids = rows.map(q => q.id)
    ;[ids[index], ids[target]] = [ids[target], ids[index]]
    try { if (draft) await draft.write('question_order', { ids }); else queryClient.setQueryData(questionKey, await orderQuestions(room.id, ids)) }
    catch (err) { setError(learningError(err, 'reorder questions')) }
    finally { operation.current = false; setBusy(false) }
  }
  const remove = async (question: Question) => {
    if (!await confirm({ title: 'Remove question?', message: draft ? 'This removes it from the working copy. Published content stays unchanged until you publish. Historical learner credit is retained.' : 'This question will be removed from the room. Questions with earned learner credit cannot be deleted.', label: 'Remove question', danger: true })) return
    if (operation.current) return
    operation.current = true; setBusy(true); setError('')
    try { if (draft) await draft.write('question_remove', { id: question.id }); else { await deleteQuestion(room.id, question.id); await questions.refetch() } }
    catch (err) { setError(learningError(err, 'delete the question')) }
    finally { operation.current = false; setBusy(false) }
  }
  const changeTaskOrder = async (index: number, direction: number) => {
    if (operation.current) return
    operation.current = true; setBusy(true); setError('')
    const ids = taskRows.map(t => t.id); [ids[index], ids[index + direction]] = [ids[index + direction], ids[index]]
    try { if (draft) await draft.write('task_order', { ids }); else queryClient.setQueryData(['room-tasks', room.id, user?.id], await orderTasks(room.id, ids)) }
    catch (err) { setError(learningError(err, 'reorder tasks')) } finally { operation.current = false; setBusy(false) }
  }
  const removeTask = async (task: RoomTask) => {
    if (!await confirm({ title: 'Remove task section?', message: 'All questions stay in the room and move to Ungrouped. Their answers, points and saved progress remain unchanged.', label: 'Remove task' })) return
    if (operation.current) return
    operation.current = true; setBusy(true); setError('')
    try { if (draft) await draft.write('task_remove', { id: task.id }); else { await deleteTask(room.id, task.id); await reload() } }
    catch (err) { setError(learningError(err, 'remove the task')) } finally { operation.current = false; setBusy(false) }
  }
  return <section className="panel question-workspace" id="challenge-questions">
    <header className="question-heading"><h2 className="panel-title">Tasks &amp; questions</h2><div className="question-actions">
      {author && <><button type="button" className="btn btn-secondary btn-sm" disabled={busy || taskRows.length >= 30 || !!tasks.error || tasks.isLoading} onClick={() => setTaskEditor('new')}>Add task</button><button type="button" className="btn btn-secondary btn-sm" onClick={() => setPreview(!preview)}>{preview ? 'Back to editor' : 'Preview questions'}</button><button type="button" className="btn btn-primary btn-sm" disabled={busy || locked || rows.length >= 100 || !!questions.error || questions.isLoading} onClick={() => setEditor('new')}><Icon name="plus" size={15} /> Add question</button></>}
      {learner && <ContextHelp label="Answer format" text="Answers are case-sensitive. Leading and trailing whitespace is ignored; internal whitespace must match." />}
      <button type="button" className="btn btn-ghost btn-sm" disabled={questions.isFetching || learner && progress.isFetching} onClick={() => { void reload(); if (learner) void progress.refetch() }} aria-label="Refresh questions and progress"><Icon name="refresh" size={16} /></button>
    </div></header>
    {error && <div className="error" role="alert">{error}</div>}
    {tasks.error && <div className="error" role="alert">{learningError(tasks.error, 'load tasks')}</div>}
    {questions.error && <div className="error" role="alert">{learningError(questions.error, 'load questions')}</div>}
    {learner && progress.isLoading && <p className="inline-loading" role="status"><span className="spinner" aria-hidden="true" /> Loading your progress…</p>}
    {learner && progress.error && <div className="error" role="alert">{learningError(progress.error, 'load your progress')}</div>}
    {questions.isLoading || tasks.isLoading ? <p className="inline-loading" role="status"><span className="spinner" aria-hidden="true" /> Loading tasks and questions…</p> : !questions.error && !tasks.error && <>
      {author && rows.length >= 100 && <p className="question-policy">This room has reached the limit of 100 questions.</p>}
      {author && locked && <p className="question-policy">Learner credit is recorded. Answers, points and the question set are locked. You can still clarify text and hints or change the order.</p>}
      {author && draft && <details className="context-help"><summary>How updates affect learner credit</summary><p>Changes stay private until published. Wording, hints and ordering preserve solves. Replacing an answer or changing points creates a new graded question; earlier points and completion records stay earned. New questions can be solved once for new credit.</p></details>}
      {author && preview && <p className="muted">Non-scoring preview · your own room cannot award points or achievements.</p>}
      {learner && accepted.status === 'error' && <p className="error" role="alert">Saved answers could not be loaded. Your earned credit is unchanged. <button type="button" className="btn btn-ghost btn-sm" onClick={() => setAnswerReload(answerReload + 1)}>Retry saved answers</button></p>}
      {!rows.length && <p className="muted">{author ? 'Add questions and answer flags to this room.' : 'No questions have been added to this room.'}</p>}
      {groups.map((group, taskIndex) => {
        const solvedCount = group.questions.filter(q => progress.data?.solved_question_ids.includes(q.id)).length
        const taskDone = group.questions.length > 0 && solvedCount === group.questions.length
        return <details className={`learning-task${taskDone ? ' is-complete' : ''}`} id={`room-task-${group.key}`} key={group.key} open={learner ? activeTask === group.key : !author || taskIndex === 0}>
          <summary onClick={learner ? event => { event.preventDefault(); chooseTask(activeTask === group.key ? null : group.key) } : undefined} onKeyDown={learner ? event => {
            if (!['ArrowDown','ArrowUp','Home','End'].includes(event.key)) return
            event.preventDefault()
            const index = event.key === 'Home' ? 0 : event.key === 'End' ? groups.length - 1 : (taskIndex + (event.key === 'ArrowDown' ? 1 : -1) + groups.length) % groups.length
            document.querySelector<HTMLElement>(`#room-task-${groups[index].key} > summary`)?.focus()
          } : undefined}><span className="task-number">{String(taskIndex + 1).padStart(2, '0')}</span><span className="task-summary-title">{group.title}</span><span className="muted">{learner ? `${solvedCount}/${group.questions.length} solved` : `${group.questions.length} questions`}</span>{taskDone && learner && <span className="badge badge-available">Task complete</span>}</summary>
          <div className="task-content">{group.task && <SafeMarkdown text={group.task.instructions} />}
          {author && !preview && group.task && <div className="question-actions"><button className="btn btn-secondary btn-sm" onClick={() => setTaskEditor(group.task)}>Edit task</button><button className="btn btn-ghost btn-sm" disabled={busy || taskIndex === 0} onClick={() => void changeTaskOrder(taskIndex, -1)}>Task up</button><button className="btn btn-ghost btn-sm" disabled={busy || taskIndex === taskRows.length - 1} onClick={() => void changeTaskOrder(taskIndex, 1)}>Task down</button><button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void removeTask(group.task!)}>Remove task</button></div>}
          {!group.questions.length && <p className="muted">{author ? 'Assign a question to this section using Add or Edit question.' : 'Reading section · no graded questions.'}</p>}
      <ol className="question-list">{group.questions.map((question, position) => { const index = rows.findIndex(q => q.id === question.id); return <li key={question.id} className="question-item">
        <div className="question-title"><span className="question-number" aria-hidden="true">{index + 1}</span><h3>{question.question_text}</h3>{question.hint && <ContextHelp label={`Hint for question ${index + 1}`} text={question.hint} />}<span className="meta-pill">{question.points} pts</span></div>
        {author && !preview && <div className="question-actions"><button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => setEditor(question)}>Edit question</button><button type="button" className="btn btn-ghost btn-sm" aria-label={`Move question ${index + 1} up`} disabled={busy || position === 0} onClick={() => void reorder(question.id, group.questions[position - 1].id)}><Icon name="arrow" size={15} /><span>Up</span></button><button type="button" className="btn btn-ghost btn-sm" aria-label={`Move question ${index + 1} down`} disabled={busy || position === group.questions.length - 1} onClick={() => void reorder(question.id, group.questions[position + 1].id)}>Down <Icon name="arrow" size={15} /></button><button type="button" className="btn btn-ghost btn-sm" disabled={busy || locked} onClick={() => void remove(question)}>Delete question</button></div>}
        {learner && <AnswerForm accepted={accepted.key === acceptedKey ? accepted.rows.find(a => a.question_id === question.id) : undefined} acceptedStatus={accepted.key === acceptedKey ? accepted.status : 'loading'} question={question} solved={progress.data?.solved_question_ids.includes(question.id) ?? false} disabled={!progress.data || !!progress.error} roomId={room.id} onProgress={async value => { await queryClient.cancelQueries({ queryKey: progressKey }); queryClient.setQueryData(progressKey, value); void queryClient.invalidateQueries({ queryKey: ['leaderboard'] }); void queryClient.invalidateQueries({ queryKey: ['my-rank'] }); void queryClient.invalidateQueries({ queryKey: ['public-learner'] }); void queryClient.invalidateQueries({ queryKey: ['learning'] }); void queryClient.invalidateQueries({ queryKey: ['learning-activity'] }) }} />}
      </li> })}</ol>{learner && taskIndex < groups.length - 1 && (taskDone || !group.questions.length) && <div className="task-next"><button type="button" className="btn btn-secondary btn-sm" onClick={() => chooseTask(groups[taskIndex + 1].key, true)}>Next task <Icon name="arrow" size={15} /></button></div>}</div></details> })}
    </>}
    {taskEditor && <TaskEditor draft={draft} roomId={room.id} task={taskEditor === 'new' ? null : taskEditor} onClose={() => setTaskEditor(null)} onSaved={async () => { await reload(); setTaskEditor(null) }} />}
    {editor && <QuestionEditor draft={draft} tasks={taskRows} roomId={room.id} question={editor === 'new' ? null : editor} locked={locked} onClose={() => setEditor(null)} onSaved={async () => { await reload(); setEditor(null) }} />}
  </section>
}

function QuestionEditor({ roomId, question, locked, onClose, onSaved, tasks = [], draft }: {tasks?: RoomTask[]; roomId: number; question: Question | null; locked: boolean; onClose: () => void; onSaved: () => Promise<void>; draft?: DraftEditor }) {
  const revision = useRef(draft?.data.revision)
  const fromDraft = !!draft
  const [answer, setAnswer] = useState(''), [initialAnswer, setInitialAnswer] = useState('')
  const [answerLoading, setAnswerLoading] = useState(!!question), [legacy, setLegacy] = useState(false), [answerError, setAnswerError] = useState(false), [retryAnswer, setRetryAnswer] = useState(0)
  useEffect(() => {
    if (!question) return
    const controller = new AbortController()
    setAnswerLoading(true); setAnswerError(false)
    void getEditableAnswer(roomId, question.id, fromDraft, revision.current, controller.signal).then(result => {
      if (controller.signal.aborted) return
      setAnswer(result.answer ?? ''); setInitialAnswer(result.answer ?? ''); setLegacy(result.legacy); setAnswerLoading(false)
    }).catch(() => { if (!controller.signal.aborted) { setAnswerError(true); setAnswerLoading(false) } })
    return () => controller.abort()
  }, [roomId, question, fromDraft, retryAnswer])
  const [pending, setPending] = useState(false), [error, setError] = useState('')
  const operation = useRef(false)
  const [dirty, setDirty] = useState(false)
  useUnsavedChanges(dirty)
  const close = () => { if (confirmDiscard(dirty)) onClose() }
  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (operation.current || answerLoading || answerError) return
    const form = event.currentTarget
    const fields = new FormData(form)
    const data: QuestionWrite = { question_text: String(fields.get('question_text')).trim(), hint: String(fields.get('hint') ?? '').trim() || null, task_id: fields.get('task_id') ? Number(fields.get('task_id')) : null }
    if (!locked) {
      data.points = Number(fields.get('points'))
      if (answer.trim() && (!question || legacy || answer !== initialAnswer)) data.answer = answer
    }
    operation.current = true; setPending(true); setError('')
    try {
      if (draft) await draft.write(question ? 'question_update' : 'question_create', question ? { id: question.id, ...data } : data, revision.current)
      else if (question) await updateQuestion(roomId, question.id, data)
      else await createQuestion(roomId, data)
      form.reset(); setDirty(false); await onSaved()
    } catch (err) { setError(learningError(err, 'save the question')) }
    finally { delete data.answer; operation.current = false; setPending(false) }
  }
  return <Dialog title={question ? 'Edit question' : 'Add question'} onClose={close} busy={pending}><form onInput={() => setDirty(true)} onSubmit={event => void save(event)}>
    <fieldset className="modal-body modal-fields" disabled={pending}>
      {error && <div className="error" role="alert">{error}</div>}
      <div className="form-group"><label htmlFor="question-text">Question text</label><textarea id="question-text" name="question_text" defaultValue={question?.question_text ?? ''} rows={2} required maxLength={1000} /></div>
      <div className="form-group"><label htmlFor="question-hint">Hint or description (optional)</label><textarea id="question-hint" name="hint" defaultValue={question?.hint ?? ''} rows={2} maxLength={2000} /></div>
      <div className="form-group"><label htmlFor="question-task">Task section</label><select id="question-task" name="task_id" defaultValue={question?.task_id ?? ''}><option value="">Ungrouped</option>{tasks.map(t => <option key={t.id} value={t.id}>{t.title}</option>)}</select></div>
      <div className="form-group"><label htmlFor="question-answer">Correct answer</label>
        {answerLoading && <p className="muted" role="status">Opening saved answer…</p>}
        {answerError && <p className="error" role="alert">Could not open the saved answer. Close and reopen this editor if the draft changed. <button type="button" onClick={() => setRetryAnswer(retryAnswer + 1)}>Retry</button></p>}
        <input id="question-answer" name="answer" type="text" autoComplete="off" spellCheck={false} maxLength={512} required={!legacy} disabled={locked || answerLoading || answerError} value={answer} onChange={event => setAnswer(event.target.value)} aria-describedby={legacy ? 'legacy-answer-help' : undefined} />
        {legacy && <p id="legacy-answer-help" className="form-hint">This older answer cannot be recovered. Enter it once for prefilled editing next time, or leave blank to keep the existing answer.</p>}
      </div>
      <div className="form-group"><label htmlFor="question-points">Points</label><input id="question-points" name="points" type="number" defaultValue={question?.points ?? 100} required min={1} max={1000} step={1} disabled={locked} /></div>
    </fieldset><footer className="modal-footer"><button type="button" className="btn btn-secondary" onClick={close} disabled={pending}>Cancel</button><button type="submit" className="btn btn-primary" disabled={pending || answerLoading || answerError}>{pending ? 'Saving…' : 'Save question'}</button></footer>
  </form></Dialog>
}

function AnswerForm({ question, solved, disabled, roomId, onProgress, accepted, acceptedStatus = 'ready' }: { question: Question; solved: boolean; disabled: boolean; roomId: number; accepted?: AcceptedAnswer; acceptedStatus?: 'loading' | 'ready' | 'error'; onProgress: (value: import('../types/api').RoomProgress) => Promise<void> }) {
  const [value, setValue] = useState(''), [acceptedHere, setAcceptedHere] = useState(false)
  const [pending, setPending] = useState(false), [feedback, setFeedback] = useState(''), [error, setError] = useState('')
  const operation = useRef(false)
  const send = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (operation.current || solved || disabled) return
    operation.current = true; setPending(true); setFeedback(''); setError('')
    try {
      const result = await submitAnswer(roomId, question.id, value)
      if (result.correct && !result.already_solved) setAcceptedHere(true)
      setFeedback(result.correct ? '' : 'Incorrect answer. Try again.')
      await onProgress(result.progress)
    } catch (err) { setError((err as { response?: { status?: number } })?.response?.status === 429 ? 'Too many attempts. Wait a minute and try again.' : learningError(err, 'submit your answer')) }
    finally { operation.current = false; setPending(false) }
  }
  const saved = accepted?.answer ?? (acceptedHere ? value : '')
  const solvedLabel = accepted?.legacy ? 'Solved — Previous answer unavailable' : accepted?.answer != null || acceptedHere ? `Solved — ${accepted?.points_awarded ?? question.points} points earned.` : acceptedStatus === 'loading' ? 'Solved — Loading saved answer…' : 'Solved — Saved answer unavailable'
  return <div className={`answer-workspace${solved ? ' answer-solved' : ''}`}>
    <form className="answer-form" onSubmit={event => void send(event)}><label className={solved ? 'solved-answer-label' : 'sr-only'} htmlFor={`answer-${question.id}`}>{solved ? 'Correct answer' : `Answer for question ${question.display_order}`}</label><input id={`answer-${question.id}`} name="answer" type="text" placeholder={solved ? 'Previous answer unavailable' : 'Your answer or flag'} autoComplete="off" required={!solved} maxLength={512} value={solved ? saved : value} onChange={event => setValue(event.target.value)} disabled={solved || pending || disabled} aria-describedby={`answer-feedback-${question.id}`} />{!solved && <button type="submit" className="btn btn-primary" disabled={pending || disabled}>{pending ? 'Submitting…' : 'Submit answer'}</button>}</form>
    <p id={`answer-feedback-${question.id}`} className={`answer-feedback${solved ? ' is-correct' : ''}`} role="status">{solved ? solvedLabel : feedback}</p>
    {error && <div className="error" role="alert">{error}</div>}
  </div>
}
