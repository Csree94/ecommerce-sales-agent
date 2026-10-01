import { useCallback, useEffect, useState } from "react";
import { fetchAdminInventory } from "./api";
import type { InventoryItem } from "./types";

/**
 * Inventory view (read-only): real Inventra stock levels proxied through the
 * Project 1 backend. Search, stock-status filter and low-stock toggle. No
 * write actions here — stock adjustments stay in Inventra's own UI.
 */
export function InventoryPage() {
  const [items, setItems] = useState<InventoryItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [stockStatus, setStockStatus] = useState("");
  const [lowStockOnly, setLowStockOnly] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchAdminInventory({
        search: search || null,
        stock_status: stockStatus || null,
        low_stock_only: lowStockOnly,
      });
      setItems(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load inventory");
    } finally {
      setLoading(false);
    }
  }, [search, stockStatus, lowStockOnly]);

  useEffect(() => {
    void load();
  }, [load]);

  function applySearch(event: React.FormEvent) {
    event.preventDefault();
    setSearch(searchInput.trim());
  }

  return (
    <section className="products-pane">
      <header className="pane-header">
        <h2>Inventory</h2>
        <span className="count">{items ? `${items.length} items` : ""}</span>
      </header>

      <form className="products-filters" onSubmit={applySearch}>
        <input
          type="search"
          placeholder="Search by name or SKU…"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          aria-label="Search inventory"
        />
        <select
          value={stockStatus}
          onChange={(e) => setStockStatus(e.target.value)}
          aria-label="Filter by stock level"
        >
          <option value="">All stock levels</option>
          <option value="in_stock">In stock</option>
          <option value="out_of_stock">Out of stock</option>
        </select>
        <label className="check-inline">
          <input
            type="checkbox"
            checked={lowStockOnly}
            onChange={(e) => setLowStockOnly(e.target.checked)}
          />
          Low stock only
        </label>
        <button type="submit">Search</button>
      </form>

      {loading && items === null && <p className="state">Loading inventory…</p>}

      {error && (
        <div className="state error">
          <p role="alert">{error}</p>
          <button onClick={() => void load()}>Retry</button>
        </div>
      )}

      {!loading && !error && items?.length === 0 && (
        <p className="state">No inventory records found.</p>
      )}

      {items && items.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Product</th>
                <th>SKU</th>
                <th className="num">Quantity</th>
                <th className="num">Threshold</th>
                <th>Status</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td>{item.product_name ?? `Product #${item.product_id}`}</td>
                  <td className="mono">{item.product_sku ?? "—"}</td>
                  <td className="num">{item.quantity}</td>
                  <td className="num">{item.low_stock_threshold}</td>
                  <td>
                    {item.quantity === 0 ? (
                      <span className="badge inactive">Out of stock</span>
                    ) : item.is_low_stock ? (
                      <span className="badge needs_human">Low stock</span>
                    ) : (
                      <span className="badge active">In stock</span>
                    )}
                  </td>
                  <td>{new Date(item.updated_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default InventoryPage;
