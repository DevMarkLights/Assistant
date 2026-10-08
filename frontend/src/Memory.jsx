import { useEffect, useState } from "react";

const CATEGORY_LABELS = {
  personal: "Personal",
  work: "Work",
  preference: "Preferences",
  project: "Projects",
  goal: "Goals",
  other: "Other",
};

const escapeRegExp = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// Matches the word only at the start of a word, so "pi" finds "Pi" but not "FastAPI"
const wordStart = (w) => `(?<![\\p{L}\\p{N}])${escapeRegExp(w)}`;

// Wraps each search-word match in <mark>
function Highlight({ text, words }) {
  if (words.length === 0) return text;
  const parts = text.split(new RegExp(`(${words.map(wordStart).join("|")})`, "giu"));
  return parts.map((part, i) => (i % 2 === 1 ? <mark key={i}>{part}</mark> : part));
}

export default function Memory({ api }) {
  const [facts, setFacts] = useState(null);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");

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

  const categoryOf = (f) => (CATEGORY_LABELS[f.category] ? f.category : "other");

  // A fact matches when every search word starts a word in its text or category name
  const words = query.split(/\s+/).filter(Boolean);
  const patterns = words.map((w) => new RegExp(wordStart(w), "iu"));
  const matches = (facts ?? []).filter((f) => {
    const haystack = `${f.text} ${CATEGORY_LABELS[categoryOf(f)]}`;
    return patterns.every((re) => re.test(haystack));
  });

  // Group by category, in the order of CATEGORY_LABELS; unknown categories land in "Other"
  const groups = Object.keys(CATEGORY_LABELS)
    .map((key) => ({ key, facts: matches.filter((f) => categoryOf(f) === key) }))
    .filter((g) => g.facts.length > 0);

  return (
    <div className="memory">
      <header>
        <p>
          Facts the assistant has picked up from your chats and uses in your conversations.
          Delete anything that's wrong or that you'd rather it forget.
        </p>
      </header>

      {facts?.length > 0 && (
        <div className="memory-search">
          <div className="search-field">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.5-3.5" />
            </svg>
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Escape" && setQuery("")}
              placeholder="Search memories"
              aria-label="Search memories"
              enterKeyHint="search"
            />
            {query && (
              <button className="icon-button clear" onClick={() => setQuery("")} aria-label="Clear search">
                ×
              </button>
            )}
          </div>
          {words.length > 0 && (
            <p className="search-count">
              {matches.length} of {facts.length} {facts.length === 1 ? "fact" : "facts"}
            </p>
          )}
        </div>
      )}

      {error && <p className="error">{error}</p>}

      {facts === null && !error && <p className="empty">Loading...</p>}
      {facts?.length === 0 && (
        <p className="empty">Nothing yet. Facts appear here as you tell the assistant about yourself.</p>
      )}
      {facts?.length > 0 && matches.length === 0 && (
        <p className="empty">No memories match "{query.trim()}".</p>
      )}

      {groups.map((g) => (
        <section key={g.key}>
          <h3>{CATEGORY_LABELS[g.key]}</h3>
          <ul>
            {g.facts.map((f) => (
              <li key={f.id}>
                <span className="text">
                  <Highlight text={f.text} words={words} />
                </span>
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
