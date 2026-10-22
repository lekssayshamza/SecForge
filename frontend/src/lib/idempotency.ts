export function newIdempotencyKey(): string {
  const cryptoObj = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined
  if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
    return cryptoObj.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

export function hasHttpResponse(error: unknown): boolean {
  return Boolean(
    error && typeof error === 'object' && 'response' in error && (error as { response?: unknown }).response,
  )
}

export function isReusableCaptureStatus(status: number): boolean {
  // 5xx: the request may have been accepted even though the response was lost.
  // 409: the backend did not start (or not yet committed) a capture for this
  // key, or one is still in progress. Because the backend keys every capture
  // by (room, creator, idempotency key), replaying the same key can only
  // resume the same logical intent and can never start an unrelated capture.
  return status === 409 || status >= 500
}

export function shouldReuseCaptureKey(error: unknown): boolean {
  // Whether the request should be automatically retried. Kept conservative:
  // auto-retry only when the backend may have accepted the request (transport
  // failure or 5xx). 409s are not auto-retried; the key is retained instead so
  // a later manual retry reuses the same intent.
  if (!error) {
    return false
  }
  if (!hasHttpResponse(error)) {
    return true
  }
  const status = (error as { response?: { status?: number } }).response?.status
  return typeof status === 'number' && status >= 500
}

export function shouldRetainCaptureKey(error: unknown): boolean {
  // Whether the capture intent is still unresolved and a subsequent retry must
  // reuse the same idempotency key rather than mint a new one. True for
  // transport failures, 5xx, and 409; all other terminal outcomes clear the
  // key so the next save is a deliberate new capture.
  if (!error) {
    return false
  }
  if (!hasHttpResponse(error)) {
    return true
  }
  const status = (error as { response?: { status?: number } }).response?.status
  return typeof status === 'number' && isReusableCaptureStatus(status)
}