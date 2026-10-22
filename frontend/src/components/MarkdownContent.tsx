import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import SecureRoomImage, { roomImageSource } from './SecureRoomImage'
import { safeInstructionLink } from '../lib/instructionLinks'
/** Raw HTML disabled. Only authenticated, room-scoped uploaded images are rendered. */
export default function SafeMarkdown({ text }: { text: string }) {
  return <div className="instruction-markdown"><Markdown remarkPlugins={[remarkGfm]} rehypePlugins={[[rehypeHighlight, { detect: false }]]} skipHtml
    urlTransform={(url, key) => key === 'src' ? (roomImageSource(url) ? url : '') : (safeInstructionLink(url) ?? '')}
    components={{
      h1: ({ children }) => <h3>{children}</h3>, h2: ({ children }) => <h4>{children}</h4>,
      a: ({ href, children }) => href ? <a href={href} rel="noopener noreferrer" target={href.startsWith('/') ? undefined : '_blank'}>{children}</a> : <span>{children}</span>,
      img: ({ src, alt }) => src && alt?.trim() ? <SecureRoomImage src={src} alt={alt} /> : <span>[Image unavailable: use an uploaded image with alt text]</span>,
      table: ({ children }) => <div className="markdown-table-scroll" role="region" aria-label="Lesson table, scroll horizontally if needed" tabIndex={0}><table>{children}</table></div>,
    }}>{text.slice(0, 12000)}</Markdown></div>
}
