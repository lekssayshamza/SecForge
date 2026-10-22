import { Link } from 'react-router-dom'
import Logo from '../components/Logo'
import Icon, { type IconName } from '../components/Icon'
import CyberHero from '../components/CyberHero'
import './Landing.css'

const steps: { title: string; detail: string; icon: IconName }[] = [
  { title: 'Discover a room', detail: 'Pick a challenge and get your bearings with a focused briefing.', icon: 'compass' },
  { title: 'Launch your machine', detail: 'Spin up the room’s private Linux environment when you’re ready.', icon: 'terminal' },
  { title: 'Connect through VPN', detail: 'Use your personal VPN profile to reach your assigned machine.', icon: 'network' },
  { title: 'Find the answers', detail: 'Use your terminal, investigate the target and uncover the flags.', icon: 'search' },
  { title: 'Earn your points', detail: 'Submit your answers and keep your progress between sessions.', icon: 'check' },
]

export default function Landing() {
  return <div className="landing-page">
    <main id="main-content" className="workspace landing-content" tabIndex={-1}>
      <section className="landing-hero" aria-labelledby="landing-title">
        <div className="landing-hero-copy">
          <p className="landing-kicker"><span aria-hidden="true" /> Cybersecurity, through practice</p>
          <h1 id="landing-title">Learn cybersecurity.<br /><span>Make it practical.</span></h1>
          <p className="landing-intro">Go beyond reading about security. Follow focused room briefings, explore real Linux machines and solve challenges with your own tools.</p>
          <div className="landing-actions">
            <Link to="/register" className="btn btn-primary">Start learning <Icon name="arrow" size={18} /></Link>
            <Link to="/rooms" className="btn btn-secondary"><Icon name="compass" size={18} /> Explore rooms</Link>
          </div>
          <p className="landing-access-note">Explore freely. Sign in when you’re ready to take part.</p>
          <ul className="landing-hero-details" aria-label="Learning experience">
            <li><Icon name="terminal" size={16} /> Real Linux labs</li>
            <li><Icon name="shield" size={16} /> Private VPN access</li>
            <li><Icon name="check" size={16} /> Saved progress</li>
          </ul>
        </div>
        <CyberHero />
      </section>
      <section className="landing-practice" aria-labelledby="practice-title">
        <div className="landing-practice-heading"><p className="eyebrow">Less watching. More doing.</p><h2 id="practice-title">The terminal is your classroom.</h2></div>
        <div className="landing-practice-items">
          <article><span className="landing-feature-icon"><Icon name="code" size={22} /></span><h3>Learn with a purpose</h3><p>A clear briefing, questions and hints give every investigation direction.</p></article>
          <article><span className="landing-feature-icon"><Icon name="terminal" size={22} /></span><h3>Work on real machines</h3><p>Connect through VPN and SSH. Use the same tools you’re learning to understand.</p></article>
          <article><span className="landing-feature-icon"><Icon name="key" size={22} /></span><h3>Turn discoveries into progress</h3><p>Find flags, get answer feedback and build up points as you complete a room.</p></article>
        </div>
      </section>
      <section id="how-it-works" className="landing-workflow" aria-labelledby="workflow-title">
        <div className="landing-section-heading"><div><p className="eyebrow">Your next challenge starts here</p><h2 id="workflow-title">From first connection to final flag.</h2></div><span className="landing-section-aside">Your tools. Your discoveries.</span></div>
        <ol className="landing-steps">{steps.map((step, index) => <li key={step.title}><div className="landing-step-icon"><span>{String(index + 1).padStart(2, '0')}</span><Icon name={step.icon} size={21} /></div><h3>{step.title}</h3><p>{step.detail}</p></li>)}</ol>
      </section>
      <section className="landing-invitation" aria-labelledby="invitation-title">
        <div><p className="eyebrow">Make your next discovery</p><h2 id="invitation-title">A little curiosity. A lot of hands-on learning.</h2><p>Choose a room, open your terminal and see what you can uncover.</p></div>
        <Link to="/register" className="btn btn-primary">Get started <Icon name="arrow" size={18} /></Link>
      </section>
    </main>
    <footer className="landing-footer"><Link to="/" className="landing-footer-brand" aria-label="SecForge home"><Logo size={22} /><span>SecForge</span></Link><span>Cybersecurity through practice.</span><div><Link to="/leaderboard">Leaderboard</Link><Link to="/login">Log In</Link><Link to="/register">Sign Up <Icon name="arrow" size={14} /></Link></div></footer>
  </div>
}
