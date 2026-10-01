import { useCallback, useEffect, useState } from "react";
import { fetchAdminDashboardStats } from "./api";
import type { DashboardMovement, InventraDashboardStats } from "./types";

function movementLabel(type: DashboardMovement["movement_type"]): string {
  if (type === "STOCK_IN") return "Stock In";
  if (type === "STOCK_ADJUSTMENT") return "Adjustment";
  return "Stock Out";
}

function formatQty(movement: DashboardMovement): string {
  if (movement.movement_type === "STOCK_IN") return `+${movement.quantity}`;
  if (movement.movement_type === "STOCK_ADJUSTMENT") return `±${movement.quantity}`;
  return `−${movement.quantity}`;
}

/** Overview (read-only): aggregate Inventra statistics — counts, low-stock
 *  alerts and the most recent stock movements. Upstream caches this for 120 s. */
export function OverviewPage() {
  const [stats, setStats] = useState<InventraDashboardStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setStats(await fetchAdminDashboardStats());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load statistics");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const cards = stats
    ? [
        { label: "Products", value: stats.total_products, sub: `${stats.active_products} active` },
        { label: "Total Stock", value: stats.total_stock, sub: "units on hand" },
        { label: "Low Stock", value: stats.low_stock_count, sub: "items need attention" },
        { label: "Out of Stock", value: stats.out_of_stock_count, sub: "items depleted" },
        { label: "Stock In (all time)", value: stats.total_stock_in, sub: "units received" },
        { label: "Stock Out (all time)", value: stats.total_stock_out, sub: "units removed" },
      ]
    : [];

  return (
    <section className="products-pane">
      <header className="pane-header">
        <h2>Overview</h2>
        <button onClick={() => void load()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </header>

      {error && (
        <div className="state error">
          <p role="alert">{error}</p>
          <button onClick={() => void load()}>Retry</button>
        </div>
      )}

      {stats && (
        <>
          <div className="stat-grid">
            {cards.map((card) => (
              <div key={card.label} className="stat-card">
                <p className="stat-label">{card.label}</p>
                <p className="stat-value">{card.value.toLocaleString()}</p>
                <p className="stat-sub">{card.sub}</p>
              </div>
            ))}
          </div>

          <div className="overview-columns">
            <div className="overview-box">
              <h3>Low Stock Alerts</h3>
              {stats.low_stock_products.length === 0 ? (
                <p className="state">No low stock items.</p>
              ) : (
                <ul className="mini-list">
                  {stats.low_stock_products.map((item) => (
                    <li key={item.product_id}>
                      <div>
                        <strong>{item.product_name}</strong>
                        <span className="mono">{item.product_sku}</span>
                      </div>
                      <span className="badge needs_human">
                        {item.quantity} left (min {item.threshold})
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="overview-box">
              <h3>Recent Movements</h3>
              {stats.recent_movements.length === 0 ? (
                <p className="state">No recent movements.</p>
              ) : (
                <ul className="mini-list">
                  {stats.recent_movements.map((movement) => (
                    <li key={movement.id}>
                      <div>
                        <strong>{movement.product_name}</strong>
                        <span>{movement.username ?? "—"}</span>
                      </div>
                      <span className="badge inactive">
                        {movementLabel(movement.movement_type)} {formatQty(movement)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </>
      )}
    </section>
  );
}

export default OverviewPage;
