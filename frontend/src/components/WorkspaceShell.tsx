import type { ReactNode } from 'react'
/** Centered content using the same responsive container as the rest of SecForge. */
export default function WorkspaceShell({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`workspace ${className}`}>{children}</div>
}
