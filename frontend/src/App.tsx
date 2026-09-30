import { useEffect, useState } from "react";
import { getStoredToken, getStoredUsername, logout } from "./api";
import { ConversationList } from "./ConversationList";
import { LoginPage } from "./LoginPage";
import { MessageThread } from "./MessageThread";
import type { ConversationSummary } from "./types";

export function App() {
  const [token, setToken] = useState<string | null>(getStoredToken());
  const [username, setUsername] = useState<string | null>(getStoredUsername());
  const [selected, setSelected] = useState<ConversationSummary | null>(null);

  // Session-storage tokens die with the tab; treat unparseable/missing state as logged out.
  useEffect(() => {
    if (sessionStorage.getItem("admin_access_token") && !token) {
      logout();
    }
  }, [token]);

  function handleLoggedIn(name: string) {
    setToken(getStoredToken());
    setUsername(name);
  }

  function handleLogout() {
    logout();
    setToken(null);
    setUsername(null);
    setSelected(null);
  }

  if (!token) {
    return <LoginPage onLoggedIn={handleLoggedIn} />;
  }

  return (
    <div className="app-shell">
      <header className="app-bar">
        <h1>Sales Agent Admin</h1>
        {username && <span className="who">signed in as {username}</span>}
        <button onClick={handleLogout}>Log out</button>
      </header>
      <main className="two-pane">
        <ConversationList selectedId={selected?.id ?? null} onSelect={setSelected} />
        <MessageThread conversation={selected} />
      </main>
    </div>
  );
}

export default App;
