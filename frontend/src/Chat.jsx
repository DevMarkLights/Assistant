import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import Memory from "./Memory";
import { useTheme } from "./theme";

const mobile = window.matchMedia("(max-width: 768px)");

const MenuIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <path d="M4 6h16M4 12h16M4 18h16" />
  </svg>
);
const PlusIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <path d="M12 5v14M5 12h14" />
  </svg>
);
const SunIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </svg>
);
const PencilIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" />
  </svg>
);

// GFM adds tables, strikethrough and task lists. Models also emit inline HTML such as <br> in
// table cells: rehype-raw parses it and rehype-sanitize (GitHub's allowlist) strips anything unsafe.
const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeRaw, rehypeSanitize];
// react-markdown passes its syntax-tree `node` to custom components; keep it off the DOM element
function domProps(props) {
  const rest = { ...props };
  delete rest.node;
  return rest;
}
const markdownComponents = {
  // Wide tables scroll sideways instead of squashing the bubble on phones
  table: (props) => (
    <div className="table-wrap">
      <table {...domProps(props)} />
    </div>
  ),
  a: (props) => <a {...domProps(props)} target="_blank" rel="noopener noreferrer" />,
};

const ClockIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </svg>
);
const MemoryIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M6 3h12v18l-6-4-6 4z" />
  </svg>
);
const ToolIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z" />
  </svg>
);
const CloudIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M17.5 19a4.5 4.5 0 1 0-1.4-8.8A6 6 0 0 0 4.5 12 3.5 3.5 0 0 0 6 19z" />
  </svg>
);
const GlobeIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
  </svg>
);
const ServerIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="4" width="18" height="7" rx="2" />
    <rect x="3" y="13" width="18" height="7" rx="2" />
    <path d="M7 7.5h.01M7 16.5h.01" />
  </svg>
);
const TOOL_ICONS = {
  get_current_time: ClockIcon,
  remember: MemoryIcon,
  forget: MemoryIcon,
  get_weather: CloudIcon,
  web_search: GlobeIcon,
  homelab_stats: ServerIcon,
};

// One chip per tool call: a spinner while it runs, then a summary that expands to show details
function ToolCalls({ calls }) {
  return (
    <div className="tool-calls">
      {calls.map((c) => {
        const Icon = TOOL_ICONS[c.name] ?? ToolIcon;
        const state = c.status === "running" ? "running" : c.ok === false ? "failed" : "done";
        const head = (
          <>
            {state === "running" ? <span className="spinner" aria-hidden="true" /> : <Icon />}
            <span>{state === "running" ? `${c.label}…` : c.summary}</span>
          </>
        );
        return c.detail && state !== "running" ? (
          <details key={c.id} className={`tool-call ${state}`}>
            <summary>{head}</summary>
            <p>{c.detail}</p>
          </details>
        ) : (
          <div key={c.id} className={`tool-call ${state}`}>
            {head}
          </div>
        );
      })}
    </div>
  );
}

// Applies one streamed event to the assistant message being built
function applyEvent(msg, ev) {
  if (ev.type === "text") return { ...msg, text: msg.text + ev.text };
  // Text the model wrote before calling a tool; the real answer follows the tool result
  if (ev.type === "retract") return { ...msg, text: msg.text.slice(0, Math.max(0, msg.text.length - ev.chars)) };
  if (ev.type === "tool_start") {
    return { ...msg, tools: [...msg.tools, { id: ev.id, name: ev.name, label: ev.label, status: "running" }] };
  }
  if (ev.type === "tool_end") {
    return {
      ...msg,
      tools: msg.tools.map((t) => (t.id === ev.id ? { ...t, ...ev, status: "done" } : t)),
    };
  }
  return msg;
}

const titleOf = (c) => c.title ?? new Date(c.created_at).toLocaleString();

// Inline title editor: Enter or clicking away saves, Escape cancels
function RenameInput({ initial, onSave, onCancel }) {
  const [value, setValue] = useState(initial);
  const done = useRef(false);

  function finish(save) {
    if (done.current) return;
    done.current = true;
    if (save) onSave(value);
    else onCancel();
  }

  return (
    <input
      className="rename-input"
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") finish(true);
        if (e.key === "Escape") finish(false);
      }}
      onBlur={() => finish(true)}
      onClick={(e) => e.stopPropagation()}
      placeholder="Chat name"
      maxLength={100}
      enterKeyHint="done"
      aria-label="Chat name"
      autoFocus
    />
  );
}

const MoonIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
  </svg>
);

export default function Chat({ user, onLogout, onUnauthorized }) {
  const [conversations, setConversations] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [showMemory, setShowMemory] = useState(false);
  // Open by default on desktop, closed (drawer hidden) on phones
  const [sidebarOpen, setSidebarOpen] = useState(() => !mobile.matches);
  const [theme, toggleTheme] = useTheme();
  // Which title is being edited: { id, place: "sidebar" | "topbar" }
  const [renaming, setRenaming] = useState(null);
  const bottomRef = useRef(null);

  // Wraps fetch so an expired session sends the user back to the login screen
  async function api(url, options) {
    const res = await fetch(url, options);
    if (res.status === 401) {
      onUnauthorized();
      throw new Error("Session expired");
    }
    return res;
  }

  useEffect(() => {
    api("/Assistant/api/conversations")
      .then((res) => res.json())
      .then(setConversations)
      .catch((e) => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Escape closes the drawer on phones
  useEffect(() => {
    if (!sidebarOpen) return;
    const onKey = (e) => e.key === "Escape" && mobile.matches && setSidebarOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sidebarOpen]);

  // On phones the drawer covers the chat, so close it after picking something
  function closeOnMobile() {
    if (mobile.matches) setSidebarOpen(false);
  }

  function openMemory() {
    setShowMemory(true);
    closeOnMobile();
  }

  async function openConversation(id) {
    setShowMemory(false);
    closeOnMobile();
    setActiveId(id);
    setError(null);
    const res = await api(`/Assistant/api/conversations/${id}/exchanges`);
    const exchanges = await res.json();
    setMessages(
      exchanges.flatMap((ex) => [
        { role: "user", text: ex.user_text },
        {
          role: "assistant",
          text: ex.assistant_text,
          tools: (ex.tool_calls ?? []).map((t, i) => ({ ...t, id: i, status: "done" })),
        },
      ])
    );
  }

  async function newConversation() {
    const res = await api("/Assistant/api/conversations", { method: "POST" });
    const conv = await res.json();
    setConversations((prev) => [conv, ...prev]);
    setShowMemory(false);
    closeOnMobile();
    setActiveId(conv.id);
    setMessages([]);
    return conv.id;
  }

  async function renameConversation(id, text) {
    setRenaming(null);
    const conv = conversations.find((c) => c.id === id);
    const title = text.trim() || null;
    if (!conv || conv.title === title) return;

    const setTitle = (t) =>
      setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, title: t } : c)));
    setTitle(title);
    try {
      const res = await api(`/Assistant/api/conversations/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title ?? "" }),
      });
      if (!res.ok) throw new Error(`Rename failed: ${res.status}`);
    } catch (err) {
      setTitle(conv.title);
      setError(err.message);
    }
  }

  function renameInput(c, place) {
    return renaming?.id === c.id && renaming.place === place ? (
      <RenameInput
        initial={c.title ?? ""}
        onSave={(text) => renameConversation(c.id, text)}
        onCancel={() => setRenaming(null)}
      />
    ) : null;
  }

  async function deleteConversation(e, id) {
    e.stopPropagation();
    if (!window.confirm("Delete this chat? Its messages will also be removed from memory.")) return;
  
    const res = await api(`/Assistant/api/conversations/${id}`, { method: "DELETE" });
    if (!res.ok) {
      setError(`Delete failed: ${res.status}`);
      return;
    }
  
    setConversations((prev) => prev.filter((c) => c.id !== id));
    if (id === activeId) {
      setActiveId(null);
      setMessages([]);
    }
  }

  async function send(e) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;

    setInput("");
    setBusy(true);
    setError(null);
    try {
      const id = activeId ?? (await newConversation());
      setMessages((prev) => [
        ...prev,
        { role: "user", text },
        { role: "assistant", text: "", tools: [] },
      ]);

      const res = await api(`/Assistant/api/conversations/${id}/chat`, {
        method: "POST",
        // Asks for the event stream; without this header the server sends plain text
        headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
        body: JSON.stringify({ message: text }),
      });
      if (!res.ok) throw new Error(`Chat failed: ${res.status}`);

      // The reply streams as newline-delimited JSON events; a read can end mid-line, so keep the tail
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        const events = lines.filter(Boolean).map((line) => JSON.parse(line));
        const failure = events.find((ev) => ev.type === "error");
        if (failure) setError(failure.message);
        setMessages((prev) => {
          const last = events.reduce(applyEvent, prev[prev.length - 1]);
          return [...prev.slice(0, -1), last];
        });
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const activeConversation = conversations.find((c) => c.id === activeId);
  const heading = showMemory ? "Memory" : activeConversation ? titleOf(activeConversation) : "New chat";

  return (
    <div className={`app ${sidebarOpen ? "sidebar-open" : ""}`}>
      <div className="backdrop" onClick={() => setSidebarOpen(false)} />

      <aside className="sidebar" aria-label="Chat history">
        <div className="sidebar-top">
          <button className="new-chat" onClick={newConversation} disabled={busy}>
            + New chat
          </button>
          <button
            className="icon-button"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close sidebar"
            title="Close sidebar"
          >
            <MenuIcon />
          </button>
        </div>
        <ul>
          {conversations.map((c) => (
            <li
              key={c.id}
              className={c.id === activeId && !showMemory ? "active" : ""}
              onClick={() => !busy && !renaming && openConversation(c.id)}
            >
              {renameInput(c, "sidebar") ?? (
                <>
                  <span className="title">{titleOf(c)}</span>
                  <button
                    className="edit icon-button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setRenaming({ id: c.id, place: "sidebar" });
                    }}
                    aria-label="Rename chat"
                    title="Rename"
                  >
                    <PencilIcon />
                  </button>
                </>
              )}
              <button
                className="delete icon-button"
                onClick={(e) => deleteConversation(e, c.id)}
                disabled={busy}
                aria-label="Delete chat"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
        <button
          className={`memory-link ${showMemory ? "active" : ""}`}
          onClick={openMemory}
          disabled={busy}
        >
          Memory
        </button>
        <div className="account">
          <span className="username">{user.username}</span>
          <button
            className="icon-button"
            onClick={toggleTheme}
            aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            title={theme === "dark" ? "Light mode" : "Dark mode"}
          >
            {theme === "dark" ? <SunIcon /> : <MoonIcon />}
          </button>
          <button className="logout" onClick={onLogout} disabled={busy}>
            Log out
          </button>
        </div>
      </aside>

      <main className="chat">
        <header className="topbar">
          {!sidebarOpen && (
            <button
              className="icon-button"
              onClick={() => setSidebarOpen(true)}
              aria-label="Open chat history"
              title="Chat history"
            >
              <MenuIcon />
            </button>
          )}
          {(activeConversation && !showMemory && renameInput(activeConversation, "topbar")) || (
            <>
              <span className="heading">{heading}</span>
              {activeConversation && !showMemory && (
                <button
                  className="icon-button rename"
                  onClick={() => setRenaming({ id: activeConversation.id, place: "topbar" })}
                  aria-label="Rename chat"
                  title="Rename"
                >
                  <PencilIcon />
                </button>
              )}
            </>
          )}
          <button
            className="icon-button new-chat-button"
            onClick={newConversation}
            disabled={busy}
            aria-label="New chat"
            title="New chat"
          >
            <PlusIcon />
          </button>
        </header>

        {showMemory ? (
          <Memory api={api} />
        ) : (
          <>
            <div className="messages">
              {messages.length === 0 && <p className="empty">Start a conversation</p>}
              {messages.map((m, i) => (
                <div key={i} className={`bubble ${m.role}`}>
                  {m.role === "assistant" ? (
                    <>
                      {m.tools?.length > 0 && <ToolCalls calls={m.tools} />}
                      {m.text ? (
                        <ReactMarkdown
                          remarkPlugins={remarkPlugins}
                          rehypePlugins={rehypePlugins}
                          components={markdownComponents}
                        >
                          {m.text}
                        </ReactMarkdown>
                      ) : busy && i === messages.length - 1 && !m.tools?.some((t) => t.status === "running") ? (
                        "..."
                      ) : null}
                    </>
                  ) : (
                    m.text
                  )}
                </div>
              ))}
              <div ref={bottomRef} />
            </div>

            {error && <p className="error">{error}</p>}

            <form className="composer" onSubmit={send}>
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="Message your assistant..."
                disabled={busy}
                autoFocus={!mobile.matches}
                enterKeyHint="send"
              />
              <button type="submit" disabled={busy || !input.trim()}>
                Send
              </button>
            </form>
          </>
        )}
      </main>
    </div>
  );
}
