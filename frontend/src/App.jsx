import { useEffect, useState } from "react";
import "./App.css";
import Chat from "./Chat";
import Login from "./Login";

export default function App() {
  // undefined = still checking the session, null = logged out
  const [user, setUser] = useState(undefined);

  useEffect(() => {
    fetch("/Assistant/api/auth/me")
      .then((res) => (res.ok ? res.json() : null))
      .then(setUser)
      .catch(() => setUser(null));
  }, []);

  async function logout() {
    await fetch("/Assistant/api/auth/logout", { method: "POST" });
    setUser(null);
  }

  if (user === undefined) return null;
  if (!user) return <Login onLogin={setUser} />;
  // key resets all chat state when a different user logs in
  return <Chat key={user.id} user={user} onLogout={logout} onUnauthorized={() => setUser(null)} />;
}
