import { useEffect, useState } from "react";

// VAPID keys are base64url; PushManager.subscribe wants raw bytes
function base64UrlToBytes(value) {
  const padded = (value + "=".repeat((4 - (value.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
const isStandalone =
  window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;

function pushSupport() {
  if ("serviceWorker" in navigator && "PushManager" in window && "Notification" in window) return "ok";
  // iOS only offers Web Push to apps added to the Home Screen and opened from there
  if (isIOS && !isStandalone) return "ios-browser";
  return "unsupported";
}

export default function Settings({ api }) {
  const support = pushSupport();
  // "checking" | "off" | "on" | "denied"
  const [state, setState] = useState(support === "ok" ? "checking" : "off");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);
  const [briefMessage, setBriefMessage] = useState(null);
  const [briefAlreadySent, setBriefAlreadySent] = useState(false);
  const [briefEnabled, setBriefEnabled] = useState(null); // null while loading

  useEffect(() => {
    api("/Assistant/api/brief/settings")
      .then((res) => res.json())
      .then((body) => setBriefEnabled(body.enabled))
      .catch((e) => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (support !== "ok") return;
    (async () => {
      if (Notification.permission === "denied") return setState("denied");
      const reg = await navigator.serviceWorker.ready;
      setState((await reg.pushManager.getSubscription()) ? "on" : "off");
    })().catch((e) => {
      setError(e.message);
      setState("off");
    });
  }, [support]);

  async function run(action) {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  // Must run straight from the tap: iOS only shows the permission prompt for a user gesture
  const enable = () =>
    run(async () => {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        setState(permission === "denied" ? "denied" : "off");
        throw new Error("Notification permission wasn't granted.");
      }
      const res = await api("/Assistant/api/push/public-key");
      if (!res.ok) throw new Error((await res.json()).detail || `Error ${res.status}`);
      const { key } = await res.json();
      const reg = await navigator.serviceWorker.ready;
      const sub =
        (await reg.pushManager.getSubscription()) ||
        (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64UrlToBytes(key) }));
      const saved = await api("/Assistant/api/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sub.toJSON()),
      });
      if (!saved.ok) throw new Error(`Couldn't save the subscription (${saved.status})`);
      setState("on");
      setMessage("Notifications are on for this device.");
    });

  const disable = () =>
    run(async () => {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        await api("/Assistant/api/push/unsubscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: sub.endpoint }),
        });
        await sub.unsubscribe();
      }
      setState("off");
      setMessage("Notifications are off for this device.");
    });

  // A delayed test lets you close the app first, which is how real notifications (and the icon
  // badge) show up
  const sendTest = (delay = 0) =>
    run(async () => {
      const res = await api(`/Assistant/api/push/test?delay=${delay}`, { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `Error ${res.status}`);
      setMessage(
        delay
          ? `Test arrives in about ${delay} seconds. Close the app now to see the banner and the icon badge.`
          : `Test sent to ${body.sent} device${body.sent === 1 ? "" : "s"}. It should arrive in a few seconds.`
      );
    });

  const toggleBrief = (enabled) =>
    run(async () => {
      setBriefEnabled(enabled);
      const res = await api("/Assistant/api/brief/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) {
        setBriefEnabled(!enabled);
        throw new Error(`Couldn't save the setting (${res.status})`);
      }
    });

  // Manual trigger for testing; the scheduled brief runs on the server every morning
  const sendBrief = (force = false) =>
    run(async () => {
      setBriefMessage(null);
      const res = await api(`/Assistant/api/brief/run?force=${force}`, { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `Error ${res.status}`);
      setBriefAlreadySent(body.status === "already_sent");
      setBriefMessage(
        body.status === "sent" ? "Brief sent. It's in the sidebar, and a notification is on its way."
          : body.status === "already_sent" ? "Today's brief was already sent."
          : "The brief failed. Check the server log (assistant.brief)."
      );
    });

  return (
    <div className="settings">
      <section>
        <h3>Notifications</h3>
        <div className="settings-card">
          {support === "ios-browser" && (
            <p>
              On iPhone, notifications only work in the installed app. Tap <strong>Share</strong>, then{" "}
              <strong>Add to Home Screen</strong>, and open the app from your Home Screen.
            </p>
          )}
          {support === "unsupported" && <p>This browser doesn't support push notifications.</p>}
          {support === "ok" && (
            <>
              <p>
                {state === "checking" && "Checking…"}
                {state === "off" && "Get notified on this device by reminders and the morning brief."}
                {state === "on" && "Notifications are on for this device."}
                {state === "denied" &&
                  "Notifications are blocked for this app. Allow them in your device's settings, then come back."}
              </p>
              <div className="settings-actions">
                {state === "off" && (
                  <button className="primary" onClick={enable} disabled={busy}>
                    Enable notifications
                  </button>
                )}
                {state === "on" && (
                  <>
                    <button className="primary" onClick={() => sendTest()} disabled={busy}>
                      Send test
                    </button>
                    <button onClick={() => sendTest(10)} disabled={busy}>
                      Send test in 10 seconds
                    </button>
                    <button onClick={disable} disabled={busy}>
                      Turn off
                    </button>
                  </>
                )}
              </div>
            </>
          )}
          {message && <p className="settings-message">{message}</p>}
          {error && <p className="error">{error}</p>}
        </div>
      </section>

      <section>
        <h3>Morning brief</h3>
        <div className="settings-card">
          <label className="toggle-row">
            <span>
              <strong>Send me a morning brief</strong>
              <span className="toggle-hint">Every morning: the weather and today's reminders.</span>
            </span>
            <input
              type="checkbox"
              role="switch"
              className="switch"
              checked={!!briefEnabled}
              disabled={busy || briefEnabled === null}
              onChange={(e) => toggleBrief(e.target.checked)}
            />
          </label>
          <div className="settings-actions">
            <button className="primary" onClick={() => sendBrief()} disabled={busy}>
              Send morning brief now
            </button>
            {briefAlreadySent && (
              <button onClick={() => sendBrief(true)} disabled={busy}>
                Send another anyway
              </button>
            )}
          </div>
          {briefMessage && <p className="settings-message">{briefMessage}</p>}
        </div>
      </section>
    </div>
  );
}
