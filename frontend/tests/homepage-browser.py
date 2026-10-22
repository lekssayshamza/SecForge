"""Local Chromium homepage acceptance. APIs are synthetic; no backend/AWS access.

Uses installed Chromium and websocket-client, with the existing browser helper.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -B tests/homepage-browser.py
Optional --output directory holds screenshots and verification.json.
"""
import argparse
import base64
import importlib.util
import json
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location('secforge_existing_browser', Path(__file__).with_name('task-a-browser.py'))
existing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(existing)


def keyboard(browser, key, code):
    for kind in ['keyDown', 'keyUp']:
        params = dict(type=kind, key=key, code=key, windowsVirtualKeyCode=code)
        if code == 13 and kind == 'keyDown': params.update(text='\r', unmodifiedText='\r')
        browser.send('Input.dispatchKeyEvent', params)


def settle_theme(browser, theme):
    browser.wait(f'document.documentElement.dataset.theme === "{theme}"')
    # Wait for finite theme transitions only; never wait for infinite scene motion.
    browser.js('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    browser.js('Promise.all(document.getAnimations().filter(a => a instanceof CSSTransition).map(a => a.finished.catch(() => {})))')


def screenshot(browser, output, name):
    browser.wait('document.fonts.status === "loaded"')
    browser.js('document.activeElement?.blur()')
    size = browser.send('Page.getLayoutMetrics')['cssContentSize']
    image = browser.send('Page.captureScreenshot', dict(captureBeyondViewport=True,
        clip=dict(x=0, y=0, width=size['width'], height=size['height'], scale=1)))['data']
    (output / name).write_bytes(base64.b64decode(image))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix='secforge-homepage-browser-'))
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    checks = []
    with existing.Browser() as b:
        b.send('Fetch.enable', {'patterns': [{'urlPattern': origin + '/api/*', 'requestStage': 'Request'} for origin in ['http://localhost:8000', 'http://127.0.0.1:8000', existing.BASE, 'https://secforgeacademy.duckdns.org']]})
        b.send('Emulation.setDeviceMetricsOverride', dict(width=1440, height=1000, deviceScaleFactor=1, mobile=False))
        b.go('/', '!!document.querySelector("#landing-title")')
        assert b.js('location.pathname === "/" && document.documentElement.dataset.theme === "light"')
        assert b.js('!document.querySelector(".landing-lab-card") && !!document.querySelector(".cyber-scene")')
        assert not b.requests, 'Public homepage requested authenticated APIs'
        b.js('window.initialPacket=getComputedStyle(document.querySelector(".cyber-packet")).strokeDashoffset')
        b.wait('getComputedStyle(document.querySelector(".cyber-packet")).strokeDashoffset !== window.initialPacket')
        checks.append('Automatic packet motion verified')
        b.send('Page.bringToFront')
        b.js('document.querySelector(".cyber-motion-control").focus()')
        keyboard(b, 'Enter', 13)
        b.wait('document.querySelector(".cyber-motion-control").getAttribute("aria-pressed") === "true"')
        assert b.js('[...document.querySelectorAll(".cyber-core,.cyber-float,.cyber-packet,.cyber-orbit-trace")].every(el=>getComputedStyle(el).animationPlayState==="paused")')
        assert b.js('new Promise(resolve=>{const el=document.querySelector(".cyber-packet");const first=getComputedStyle(el).strokeDashoffset;requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(first===getComputedStyle(el).strokeDashoffset)))})')
        checks.append('Keyboard pause freezes all animation')
        for theme in ['light', 'dark']:
            if theme == 'dark': b.click('button[aria-label="Switch to dark theme"]')
            settle_theme(b, theme)
            for width in [1440, 1024, 768, 390, 320]:
                b.send('Emulation.setDeviceMetricsOverride', dict(width=width, height=1000, deviceScaleFactor=1, mobile=width<500))
                b.wait(f'innerWidth === {width}')
                assert b.js('document.documentElement.scrollWidth <= document.documentElement.clientWidth'), f'Overflow at {theme}/{width}'
                assert b.js('(()=>{const r=document.querySelector(".cyber-scene").getBoundingClientRect();return r.width>0 && r.left>=0 && r.right<=innerWidth})()')
                assert b.js('(()=>{const r=document.querySelector(".landing-nav .btn-primary").getBoundingClientRect();return r.left>=0 && r.right<=innerWidth})()')
                screenshot(b, output, f'homepage-{theme}-{width}.png')
                checks.append(f'{theme}/{width}px: scene, controls and no overflow')
            b.send('Page.reload')
            b.wait('!!document.querySelector("#landing-title")')
            settle_theme(b, theme)
            assert b.js(f'localStorage.getItem("secforge-theme") === "{theme}"')
        checks.append('Theme persistence across reload')
        b.send('Emulation.setEmulatedMedia', {'features': [{'name':'prefers-reduced-motion','value':'reduce'}]})
        b.wait('!!document.querySelector(".cyber-motion-note") && !document.querySelector(".cyber-motion-control")')
        assert b.js('[...document.querySelectorAll(".cyber-core,.cyber-float,.cyber-packet,.cyber-orbit-trace")].every(el=>getComputedStyle(el).animationName==="none")')
        b.send('Page.reload')
        b.wait('!!document.querySelector(".cyber-motion-note")')
        checks.append('Reduced motion on change and initial load; static fallback')
        b.send('Emulation.setEmulatedMedia', {'features': [{'name':'prefers-reduced-motion','value':'no-preference'}]})
        b.wait('!!document.querySelector(".cyber-motion-control")')
        b.click('.cyber-motion-control')
        b.click('.landing-actions a[href="/register"]')
        b.wait('location.pathname === "/register" && !!document.querySelector("#email")')
        b.click('a[aria-label="SecForge home"]')
        b.wait('location.pathname === "/" && !!document.querySelector("#landing-title")')
        b.click('.landing-actions a[href="/rooms"]')
        b.wait('location.pathname === "/login" && !!document.querySelector("#username")')
        # The requested rooms destination remains in router state after the auth redirect.
        assert b.js('history.state.usr?.from?.pathname === "/rooms"')
        b.click('a[aria-label="SecForge home"]')
        b.wait('!!document.querySelector("#landing-title")')
        b.click('.landing-nav a[href="/login"]')
        b.wait('location.pathname === "/login" && !!document.querySelector("#password")')
        checks.append('Signup/login navigation and protected Explore rooms redirect')
        b.go('/', '!!document.querySelector("#landing-title")')
        b.send('Emulation.setDeviceMetricsOverride', dict(width=1440, height=1000, deviceScaleFactor=1, mobile=False))
        b.js('document.activeElement.blur()')
        # The skip link is a real focus target and activation reaches the main landmark.
        b.js('document.querySelector(".skip-link").focus()')
        keyboard(b, 'Enter', 13)
        b.wait('document.activeElement.id === "main-content"')
        checks.append('Keyboard skip link')
        # Capture final animated scene after settling both themes, at fixed positions.
        for theme in ['light', 'dark']:
            if b.js('document.documentElement.dataset.theme') != theme:
                b.click(f'button[aria-label="Switch to {theme} theme"]')
            settle_theme(b, theme)
            b.js('window.scrollTo(0,0)')
            b.click('button[aria-label="Pause illustration animation"]')
            screenshot(b, output, f'homepage-{theme}-final.png')
            b.click('button[aria-label="Play illustration animation"]')
        assert not b.errors, b.errors
        assert not b.requests, 'Public route checks caused backend requests'
    (output / 'verification.json').write_text(json.dumps({'checks':checks,'api_requests':0,'runtime_exceptions':0,'browser':'Installed Chromium via CDP'},indent=2))
    print(f'PASS: {len(checks)} homepage browser checks; no backend/API calls. Screenshots: {output}')


if __name__ == '__main__':
    main()
