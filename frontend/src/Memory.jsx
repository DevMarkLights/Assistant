import { useEffect, useState } from "react";

const CATEGORY_LABELS = {
  personal: "Personal",
  work: "Work",
  preference: "Preferences",
  project: "Projects",
  goal: "Goals",
  other: "Other",
};

export default function Memory({ api }) {
  const [facts, setFacts] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api("/Assistant/api/memory/facts")
      .then((res) => res.json())
      .then(setFacts)
      .catch((e) => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function deleteFact(fact) {
    if (!window.confirm(`Forget this?\n\n"${fact.text}"`)) return;
    try {
      const res = await api(`/Assistant/api/memory/facts/${fact.id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`Delete failed: ${res.status}`);
      setFacts((prev) => prev.filter((f) => f.id !== fact.id));
    } catch (err) {
      setError(err.message);
    }
  }

  // Group by category, in the order of CATEGORY_LABELS; unknown categories land in "Other"
  const groups = Object.keys(CATEGORY_LABELS)
    .map((key) => ({
      key,
      facts: (facts ?? []).filter((f) => (CATEGORY_LABELS[f.category] ? f.category : "other") === key),
    }))
    .filter((g) => g.facts.length > 0);

  return (
    <div className="memory">
      <header>
        <p>
          Facts the assistant has picked up from your chats. They're included in every conversation.
          Delete anything that's wrong or that you'd rather it forget.
        </p>
      </header>

      {error && <p className="error">{error}</p>}

      {facts === null && !error && <p className="empty">Loading...</p>}
      {facts?.length === 0 && (
        <p className="empty">Nothing yet. Facts appear here as you tell the assistant about yourself.</p>
      )}

      {groups.map((g) => (
        <section key={g.key}>
          <h3>{CATEGORY_LABELS[g.key]}</h3>
          <ul>
            {g.facts.map((f) => (
              <li key={f.id}>
                <span className="text">{f.text}</span>
                <span className="date">{new Date(f.updated_at).toLocaleDateString()}</span>
                <button className="delete icon-button" onClick={() => deleteFact(f)} aria-label="Delete fact">
                  ×
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
