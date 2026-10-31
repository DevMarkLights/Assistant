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

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
