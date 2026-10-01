import { useEffect, useState } from "react";
import { getStoredToken, getStoredUsername, logout } from "./api";
import { CategoriesPage } from "./CategoriesPage";
import { ConversationList } from "./ConversationList";
import { InventoryPage } from "./InventoryPage";
import { LoginPage } from "./LoginPage";
import { MessageThread } from "./MessageThread";
import { MovementsPage } from "./MovementsPage";
import { OverviewPage } from "./OverviewPage";
import { ProductsPage } from "./ProductsPage";
import type { ConversationSummary } from "./types";

type DashboardView =
  | "conversations"
  | "overview"
  | "products"
  | "inventory"
  | "movements"
  | "categories";

const INVENTRA_VIEWS: DashboardView[] = [
  "overview",
  "products",
  "inventory",
  "movements",
  "categories",
];

export function App() {
  const [token, setToken] = useState<string | null>(getStoredToken());
  const [username, setUsername] = useState<string | null>(getStoredUsername());
  const [selected, setSelected] = useState<ConversationSummary | null>(null);
  const [view, setView] = useState<DashboardView>("conversations");

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
        <nav className="tab-bar" aria-label="Dashboard sections">
          <button
            className={view === "conversations" ? "tab selected" : "tab"}
            onClick={() => setView("conversations")}
          >
            Conversations
          </button>
          {INVENTRA_VIEWS.map((inventraView) => {
            const labels: Record<DashboardView, string> = {
              conversations: "Conversations",
              overview: "Overview",
              products: "Products",
              inventory: "Inventory",
              movements: "Stock Movements",
              categories: "Categories",
            };
            return (
              <button
                key={inventraView}
                className={view === inventraView ? "tab selected" : "tab"}
                onClick={() => setView(inventraView)}
              >
                {labels[inventraView]}
              </button>
            );
          })}
        </nav>
        {username && <span className="who">signed in as {username}</span>}
        <button onClick={handleLogout}>Log out</button>
      </header>
      {view === "conversations" ? (
        <main className="two-pane">
          <ConversationList selectedId={selected?.id ?? null} onSelect={setSelected} />
          <MessageThread conversation={selected} />
        </main>
      ) : (
        <main className="products-main">
          {view === "overview" && <OverviewPage />}
          {view === "products" && <ProductsPage />}
          {view === "inventory" && <InventoryPage />}
          {view === "movements" && <MovementsPage />}
          {view === "categories" && <CategoriesPage />}
        </main>
      )}
    </div>
  );
}

export default App;
