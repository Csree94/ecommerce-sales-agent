import { useCallback, useEffect, useState } from "react";
import { fetchAdminMovements } from "./api";
import type { StockMovement } from "./types";

function movementLabel(type: StockMovement["movement_type"]): string {
  if (type === "STOCK_IN") return "Stock In";
  if (type === "STOCK_ADJUSTMENT") return "Adjustment";
  return "Stock Out";
}

/**
 * Stock Movements view (read-only): Inventra's audit history of stock changes,
 * proxied through the Project 1 backend. Filterable by movement type.
 */
export function MovementsPage() {
  const [data, setData] = useState<Awaited<ReturnType<typeof fetchAdminMovements>> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [movementType, setMovementType] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchAdminMovements({
        page,
        movement_type: movementType || null,
      });
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load stock movements");
    } finally {
      setLoading(false);
    }
  }, [page, movementType]);

  useEffect(() => {
    void load();
  }, [load]);

  const pages = data ? Math.max(1, Math.ceil(data.total / data.per_page)) : 1;

  return (
    <section className="products-pane">
      <header className="pane-header">
        <h2>Stock Movements</h2>
        <span className="count">{data ? `${data.total} total` : ""}</span>
      </header>

      <div className="products-filters">
        <select
          value={movementType}
          onChange={(e) => {
            setMovementType(e.target.value);
            setPage(1);
          }}
          aria-label="Filter by movement type"
        >
          <option value="">All movements</option>
          <option value="STOCK_IN">Stock In</option>
          <option value="STOCK_OUT">Stock Out</option>
          <option value="STOCK_ADJUSTMENT">Adjustment</option>
        </select>
      </div>

      {loading && data === null && <p className="state">Loading stock movements…</p>}

      {error && (
        <div className="state error">
          <p role="alert">{error}</p>
          <button onClick={() => void load()}>Retry</button>
        </div>
      )}

      {!loading && !error && data?.movements.length === 0 && (
        <p className="state">No stock movements found.</p>
      )}

      {data && data.movements.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Type</th>
                <th>Product</th>
                <th className="num">Quantity</th>
                <th>User</th>
                <th>Notes</th>
                <th>Date</th>
              </tr>
            </thead>
            <tbody>
              {data.movements.map((movement) => (
                <tr key={movement.id}>
                  <td>
                    <span
                      className={`badge ${
                        movement.movement_type === "STOCK_IN"
                          ? "active"
                          : movement.movement_type === "STOCK_ADJUSTMENT"
                            ? "needs_human"
                            : "inactive"
                      }`}
                    >
                      {movementLabel(movement.movement_type)}
                    </span>
                  </td>
                  <td>{movement.product_name ?? `Product #${movement.product_id}`}</td>
                  <td className="num">
                    {movement.movement_type === "STOCK_IN"
                      ? `+${movement.quantity}`
                      : movement.movement_type === "STOCK_ADJUSTMENT"
                        ? `±${movement.quantity}`
                        : `−${movement.quantity}`}
                  </td>
                  <td>{movement.username ?? "—"}</td>
                  <td>{movement.notes ?? "—"}</td>
                  <td>{new Date(movement.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data && pages > 1 && (
        <footer className="pager">
          <button disabled={loading || page <= 1} onClick={() => setPage(page - 1)}>
            ← Prev
          </button>
          <span>
            {page} / {pages}
          </span>
          <button disabled={loading || page >= pages} onClick={() => setPage(page + 1)}>
            Next →
          </button>
        </footer>
      )}
    </section>
  );
}

export default MovementsPage;
