import { useState } from "react";

export default function Login({ onLogin }) {
  const [mode, setMode] = useState("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const isRegister = mode === "register";

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/Assistant/api/auth/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        // FastAPI validation errors come back as a list
        const detail = Array.isArray(body.detail)
          ? "Password must be at least 8 characters"
          : body.detail;
        throw new Error(detail || `Request failed: ${res.status}`);
      }
      onLogin(body);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function switchMode() {
    setMode(isRegister ? "login" : "register");
    setError(null);
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <h1>{isRegister ? "Create account" : "Sign in"}</h1>

        <label>
          Username
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>

        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={isRegister ? "new-password" : "current-password"}
            minLength={isRegister ? 8 : undefined}
            required
          />
        </label>

        {error && <p className="login-error">{error}</p>}

        <button type="submit" disabled={busy}>
          {busy ? "..." : isRegister ? "Create account" : "Sign in"}
        </button>

        <button type="button" className="link" onClick={switchMode} disabled={busy}>
          {isRegister ? "Have an account? Sign in" : "No account? Create one"}
        </button>
      </form>
    </div>
  );
}
