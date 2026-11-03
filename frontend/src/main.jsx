import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'

// Service worker for push notifications (public/sw.js), scoped to the app's base path
if ('serviceWorker' in navigator) {
  navigator.serviceWorker
    .register(`${import.meta.env.BASE_URL}sw.js`, { scope: import.meta.env.BASE_URL })
    .catch((err) => console.warn('Service worker registration failed:', err))
}

// Opening the app means the user has seen their notifications: clear the Home Screen icon badge
function clearBadge() {
  if (document.visibilityState === 'visible' && navigator.clearAppBadge) {
    navigator.clearAppBadge().catch(() => {})
  }
}
clearBadge()
document.addEventListener('visibilitychange', clearBadge)

// The installed app resumes a page kept in memory instead of reloading it, so a deploy isn't
// picked up until the app is closed. When the app comes back after being away for AWAY_MS, check
// whether a new build is deployed (each build's script has a new hashed name) and reload if so.
const AWAY_MS = 60_000
const buildScript = (doc) => doc.querySelector('script[type="module"][src*="/assets/"]')?.getAttribute('src')
const runningBuild = buildScript(document)
let hiddenAt = null

async function reloadIfUpdated() {
  try {
    const res = await fetch(`${import.meta.env.BASE_URL}?build-check=${Date.now()}`, { cache: 'no-store' })
    if (!res.ok) return
    const deployed = buildScript(new DOMParser().parseFromString(await res.text(), 'text/html'))
    if (deployed && runningBuild && deployed !== runningBuild) window.location.reload()
  } catch {
    // Offline or server restarting: try again next time the app comes back
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    hiddenAt = Date.now()
  } else if (hiddenAt && Date.now() - hiddenAt >= AWAY_MS) {
    hiddenAt = null
    reloadIfUpdated()
  }
})

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
