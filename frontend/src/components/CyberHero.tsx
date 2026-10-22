import { useEffect, useId, useState } from 'react'

const motionQuery = '(prefers-reduced-motion: reduce)'

/** An illustrative network, never a projection of live labs or platform telemetry. */
export default function CyberHero() {
  const id = useId()
  const [paused, setPaused] = useState(false)
  const [reducedMotion, setReducedMotion] = useState(() =>
    typeof window !== 'undefined' && Boolean(window.matchMedia?.(motionQuery).matches),
  )
  useEffect(() => {
    if (!window.matchMedia) return
    const preference = window.matchMedia(motionQuery)
    const update = () => setReducedMotion(preference.matches)
    update()
    if (preference.addEventListener) {
      preference.addEventListener('change', update)
      return () => preference.removeEventListener('change', update)
    }
    // Older Safari exposes only the legacy MediaQueryList listener methods.
    preference.addListener(update)
    return () => preference.removeListener(update)
  }, [])

  return <figure className={`cyber-hero${paused || reducedMotion ? ' is-paused' : ''}`}>
    <svg className="cyber-scene" viewBox="0 0 620 480" role="img" aria-labelledby={`${id}-title ${id}-description`}>
      <title id={`${id}-title`}>The SecForge practice network</title>
      <desc id={`${id}-description`}>An illustrated SF cyber core connects a terminal, a Linux machine, a private VPN connection and a challenge flag. This is an illustration, not a live lab.</desc>
      <defs>
        <radialGradient id={`${id}-halo`}><stop offset="0" stopColor="var(--color-accent)" stopOpacity=".2" /><stop offset="1" stopColor="var(--color-accent)" stopOpacity="0" /></radialGradient>
        <linearGradient id={`${id}-face`} x1="0" x2="1" y1="0" y2="1"><stop stopColor="var(--scene-core-light)" /><stop offset="1" stopColor="var(--scene-core-dark)" /></linearGradient>
        <linearGradient id={`${id}-wire`} x1="0" x2="1"><stop stopColor="var(--color-accent)" stopOpacity=".35" /><stop offset=".5" stopColor="var(--color-accent)" /><stop offset="1" stopColor="var(--color-accent)" stopOpacity=".35" /></linearGradient>
      </defs>
      <g aria-hidden="true">
        <ellipse cx="318" cy="285" rx="245" ry="186" fill={`url(#${id}-halo)`} />
        <g className="cyber-grid">
          <path d="M30 300 310 138 590 300 310 462Z" />
          <path d="m76 273 280 162m-234-189 280 162m-233-189 280 162m-233-189 280 162m-233-189 280 162" />
          <path d="M77 327 357 165M123 354 403 192M170 381 450 219M217 408 497 246M263 435 543 273" />
        </g>
        <g className="cyber-orbit"><ellipse cx="310" cy="300" rx="228" ry="130" /><ellipse className="cyber-orbit-trace" cx="310" cy="300" rx="228" ry="130" pathLength="100" /></g>
        <g className="cyber-wires">
          <path d="M310 292 310 260 162 175" /><path d="M310 292 449 212 449 174" />
          <path d="M310 292 445 370 506 335" /><path d="M310 292 183 365 113 325" />
          <path d="M162 175 78 223 78 299 113 319" /><path d="M449 174 550 232 550 305 506 330" />
        </g>
        <g className="cyber-packets" stroke={`url(#${id}-wire)`}>
          <path className="cyber-packet cyber-packet-a" d="M162 175 310 260 310 292" pathLength="100" />
          <path className="cyber-packet cyber-packet-b" d="M310 292 449 212 449 174" pathLength="100" />
          <path className="cyber-packet cyber-packet-c" d="M506 335 445 370 310 292" pathLength="100" />
          <path className="cyber-packet cyber-packet-d" d="M310 292 183 365 113 325" pathLength="100" />
          <path className="cyber-packet cyber-packet-e" d="M162 175 78 223 78 299 113 319" pathLength="100" />
          <path className="cyber-packet cyber-packet-f" d="M449 174 550 232 550 305 506 330" pathLength="100" />
        </g>
        <g className="cyber-junctions"><circle cx="78" cy="223" r="4" /><circle cx="550" cy="232" r="4" /><circle cx="183" cy="365" r="4" /><circle cx="445" cy="370" r="4" /></g>
        <ellipse className="cyber-core-shadow" cx="310" cy="306" rx="90" ry="38" />
        <g className="cyber-core">
          <path className="cyber-platform-side" d="m222 281 88 51 88-51v17l-88 51-88-51Z" /><path className="cyber-platform-top" d="m222 281 88-51 88 51-88 51Z" /><path className="cyber-platform-line" d="m222 298 88 51 88-51" />
          <path className="cyber-platform-side" d="m237 254 73 42 73-42v16l-73 43-73-43Z" /><path className="cyber-platform-top" d="m237 254 73-42 73 42-73 42Z" /><path className="cyber-core-edge" d="m237 270 73 43 73-43" />
          <path fill={`url(#${id}-face)`} d="m255 171 55-32 55 32v74l-55 32-55-32Z" /><path className="cyber-core-top" d="m255 171 55-32 55 32-55 32Z" /><path className="cyber-core-seam" d="M310 203v74m-55-106 55 32 55-32" /><path className="cyber-core-edge" d="m255 245 55 32 55-32" />
          <g transform="translate(277 169) scale(2.05)"><path className="cyber-monogram-f" d="M14 4h17v6H20v4h10v6H20v8h-6Z" /><path className="cyber-monogram-s" d="M3 4h13v6H7v4h9v14H1v-6h11v-4H3Z" /></g>
          <path className="cyber-core-port" d="m267 247 19 11m48-5 9-5" /><circle className="cyber-core-light" cx="353" cy="242" r="2.5" />
        </g>
        <g transform="translate(54 96)"><g className="cyber-float cyber-float-a">
          <path className="cyber-node-depth" d="M7 10h157v105H7Z" /><rect className="cyber-terminal" width="164" height="105" rx="9" /><path className="cyber-terminal-divider" d="M0 25h164" />
          <g className="cyber-terminal-dots"><circle cx="13" cy="13" r="2.5" /><circle cx="23" cy="13" r="2.5" /><circle cx="33" cy="13" r="2.5" /></g>
          <text className="cyber-terminal-label" x="150" y="16" textAnchor="end">TERMINAL</text>
          <text className="cyber-terminal-command" x="14" y="47">$ ssh learner@lab</text><text className="cyber-terminal-output" x="14" y="67">~/challenge</text><text className="cyber-terminal-command" x="14" y="87">$ ls -la</text><rect className="cyber-cursor" x="75" y="77" width="6" height="11" rx="1" />
        </g></g>
        <g transform="translate(418 100)"><g className="cyber-float cyber-float-b">
          <path className="cyber-node-depth" d="m0 22 49-28 49 28v63l-49 28L0 85Z" /><path className="cyber-node-top" d="m0 16 49-28 49 28-49 28Z" /><path className="cyber-node-front" d="m0 16 49 28v56L0 72Z" /><path className="cyber-node-side" d="m49 44 49-28v56l-49 28Z" />
          <path className="cyber-server-slot" d="m11 38 26 15m-26-3 26 15m-26-3 26 15" /><circle className="cyber-server-light" cx="84" cy="44" r="3" /><circle className="cyber-server-light" cx="84" cy="58" r="3" /><text className="cyber-label" x="49" y="128" textAnchor="middle">LINUX MACHINE</text>
        </g></g>
        <g transform="translate(455 290)"><g className="cyber-float cyber-float-c">
          <path className="cyber-node-depth" d="m0 26 51-30 51 30v34l-51 30L0 60Z" /><path className="cyber-node-front" d="m0 26 51 30 51-30v18L51 74 0 44Z" /><path className="cyber-node-top" d="m0 26 51-30 51 30-51 30Z" />
          <rect className="cyber-lock" x="39" y="9" width="25" height="24" rx="5" /><path className="cyber-lock-loop" d="M44 9V4a8 8 0 0 1 16 0v5" /><circle className="cyber-lock-key" cx="51.5" cy="19" r="2" /><path className="cyber-lock-loop" d="M51.5 21v4" /><text className="cyber-label" x="51" y="110" textAnchor="middle">PRIVATE VPN</text>
        </g></g>
        <g transform="translate(66 286)"><g className="cyber-float cyber-float-d">
          <path className="cyber-node-front" d="m0 27 46 27 46-27v17L46 71 0 44Z" /><path className="cyber-node-top" d="m0 27 46-27 46 27-46 27Z" /><path className="cyber-flag-pole" d="M46 30v-57" /><path className="cyber-flag" d="M47-27c13-8 19 10 35 2v27c-16 8-22-10-35-2Z" /><path className="cyber-flag-detail" d="m58-9 5 5 10-10" /><text className="cyber-label" x="46" y="105" textAnchor="middle">FIND THE FLAG</text>
        </g></g>
        <text className="cyber-coordinate" x="310" y="444" textAnchor="middle">LEARN / INVESTIGATE / DISCOVER</text>
      </g>
    </svg>
    <figcaption className="cyber-hero-caption">
      <span><span className="cyber-legend-dot" aria-hidden="true" /> A private space to practise</span>
      {reducedMotion ? <span className="cyber-motion-note">Reduced motion enabled</span> :
        <button className="cyber-motion-control" type="button" onClick={() => setPaused(value => !value)} aria-pressed={paused} aria-label={paused ? 'Play illustration animation' : 'Pause illustration animation'}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true">{paused ? <path d="m3 1 8 5-8 5Z" /> : <path d="M2 1h3v10H2Zm5 0h3v10H7Z" />}</svg>
          {paused ? 'Play animation' : 'Pause animation'}
        </button>}
    </figcaption>
  </figure>
}
