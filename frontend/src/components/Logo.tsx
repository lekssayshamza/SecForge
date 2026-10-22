export default function Logo({ size = 28 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className="brand-mark">
    <path className="brand-f" d="M14 4h17v6H20v4h10v6H20v8h-6Z" />
    <path className="brand-s" d="M3 4h13v6H7v4h9v14H1v-6h11v-4H3Z" />
  </svg>
}
