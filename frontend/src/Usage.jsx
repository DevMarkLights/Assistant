import { useEffect, useState } from "react";

// The owner's view of LLM usage (GET /Assistant/api/usage): each model's budget and use, cooldowns,
// which models each task tries, and recent 429s and failovers. Refreshes while open.
const REFRESH_MS = 10000;

const TASK_LABELS = {
  chat_tools: "Chat",
  background_json: "Memory",
  titles: "Titles",
  morning_brief: "Morning brief",
};

const EVENT_LABELS = {
  rate_limited: "Rate limited",
  failover: "Failover",
  sidelined: "Provider sidelined",
  busy: "All busy",
  interrupted: "Reply cut off",
};

const number = (n) => (n >= 10000 ? `${Math.round(n / 1000)}K` : n.toLocaleString());

function duration(seconds) {
  if (seconds < 90) return `${seconds}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}

// One budget: how much is used of the limit, as a bar (no limit = just the number used)
function Meter({ label, used, limit }) {
  const fraction = limit ? Math.min(1, used / limit) : 0;
  const level = fraction >= 0.9 ? "high" : fraction >= 0.6 ? "mid" : "";
  return (
    <div className="meter">
      <div className="meter-label">
        <span>{label}</span>
        <span>{limit ? `${number(used)} / ${number(limit)}` : `${number(used)} (no limit)`}</span>
      </div>
      {limit ? (
        <div className="meter-track" role="progressbar" aria-label={label}
          aria-valuemin={0} aria-valuemax={limit} aria-valuenow={used}>
          <div className={`meter-fill ${level}`} style={{ width: `${fraction * 100}%` }} />
        </div>
      ) : null}
    </div>
  );
}

function ModelCard({ model }) {
  const { limits, remaining, used_today: today } = model;
  const usedNow = (limit, left) => (limit == null || left == null ? 0 : limit - left);
  let state = { text: "Ready", kind: "ok" };
  if (!model.has_key) state = { text: "No API key", kind: "off" };
  else if (model.sidelined_seconds) state = { text: `Provider sidelined, ${duration(model.sidelined_seconds)}`, kind: "bad" };
  else if (model.cooldown_seconds) state = { text: `Cooling down, ${duration(model.cooldown_seconds)}`, kind: "warn" };
  return (
    <div className="settings-card usage-model">
      <div className="usage-model-head">
        <div>
          <strong>{model.key}</strong>
          <span className="usage-provider">
            {model.provider}
            {model.personal_data ? "" : " · no personal data"}
          </span>
        </div>
        <span className={`usage-state ${state.kind}`}>{state.text}</span>
      </div>
      <div className="usage-meters">
        <Meter label="Tokens this minute" used={usedNow(limits.tpm, remaining.minute_tokens)} limit={limits.tpm} />
        <Meter label="Requests this minute" used={usedNow(limits.rpm, remaining.minute_requests)} limit={limits.rpm} />
        <Meter label="Tokens today" used={today.tokens} limit={limits.tpd} />
        <Meter label="Requests today" used={today.requests} limit={limits.rpd} />
      </div>
    </div>
  );
}

export default function Usage({ api }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let stopped = false;
    const load = () =>
      api("/Assistant/api/usage")
        .then((res) => {
          if (!res.ok) throw new Error(res.status === 403 ? "Only the server's owner can see usage." : `Error ${res.status}`);
          return res.json();
        })
        .then((body) => !stopped && (setData(body), setError(null)))
        .catch((e) => !stopped && setError(e.message));
    load();
    const timer = setInterval(() => document.visibilityState === "visible" && load(), REFRESH_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error && !data) return <div className="settings"><p className="settings-message">{error}</p></div>;
  if (!data) return <div className="settings"><p className="settings-message">Loading…</p></div>;
  if (data.local) {
    return <div className="settings"><p className="settings-message">LOCAL=true: replies come from Ollama, so there are no cloud limits to track.</p></div>;
  }

  return (
    <div className="settings usage">
      <section>
        <h3>Models</h3>
        <p className="usage-note">
          Today is {data.day} (UTC). Daily numbers include every server sharing the database; the
          per-minute numbers and cooldowns are this server's. Refreshes every 10 seconds.
        </p>
        <div className="usage-models">
          {data.models.map((m) => <ModelCard key={m.key} model={m} />)}
        </div>
      </section>

      <section>
        <h3>Who serves each task</h3>
        <div className="settings-card">
          {data.tasks.map((task) => (
            <div key={task.name} className="usage-task">
              <span className="usage-task-name">{TASK_LABELS[task.name] || task.name}</span>
              <span className="usage-chain">
                {task.models.map((m, i) => (
                  <span key={m.key} className={`usage-chip ${m.usable ? "" : "skipped"}`} title={m.why_not || "Used in this order"}>
                    {i > 0 && <span aria-hidden="true">→ </span>}
                    {m.key}
                    {!m.usable && <span className="sr-only"> (skipped: {m.why_not})</span>}
                  </span>
                ))}
              </span>
            </div>
          ))}
        </div>
      </section>

      <section>
        <h3>Recent events</h3>
        <div className="settings-card">
          {data.events.length === 0 ? (
            <p className="usage-note">No rate limits, failovers or errors recorded.</p>
          ) : (
            <ul className="usage-events">
              {data.events.map((e, i) => (
                <li key={i}>
                  <time dateTime={e.at}>{new Date(e.at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</time>
                  <span className={`usage-event ${e.kind}`}>{EVENT_LABELS[e.kind] || e.kind}</span>
                  <span className="usage-event-detail">
                    {[TASK_LABELS[e.task] || e.task, e.model, e.detail].filter(Boolean).join(" · ")}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        {error && <p className="settings-message">Couldn't refresh: {error}</p>}
      </section>
    </div>
  );
}
