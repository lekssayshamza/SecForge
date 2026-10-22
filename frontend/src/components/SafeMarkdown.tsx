import { lazy, Suspense } from 'react'
export { safeInstructionLink } from '../lib/instructionLinks'
// Keep the Markdown/highlighting parser out of the homepage and login bundle.
const MarkdownContent = lazy(() => import('./MarkdownContent'))
export default function SafeMarkdown({ text }: { text: string }) {
  return <Suspense fallback={<div className="instruction-markdown" role="status">Formatting lesson…</div>}><MarkdownContent text={text} /></Suspense>
}
