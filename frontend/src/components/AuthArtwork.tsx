import CyberHero from './CyberHero'
import '../pages/Landing.css'
import './Auth.css'
export default function AuthArtwork() {
  return <>
    <aside className="auth-art landing-page" aria-label="Hands-on cybersecurity learning"><p className="eyebrow">Learn. Investigate. Discover.</p>
      <h2>Your next discovery<br /><span>starts here.</span></h2><p>Real Linux machines. Private VPN access. Challenges that turn curiosity into skill.</p><CyberHero />
      <div className="auth-art-bottom"><span>Hands-on labs</span><span>Saved progress</span><span>Your tools</span></div>
    </aside></>
}
