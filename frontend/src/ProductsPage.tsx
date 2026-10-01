import { useCallback, useEffect, useState } from "react";
import { fetchAdminProducts } from "./api";
import type { ProductListResponse } from "./types";

function formatPrice(price: number): string {
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(price);
}

type ActiveFilter = "" | "true" | "false";

/**
 * Products view (Milestone 2, Step A): READ-ONLY catalog table proxied through
 * the Project 1 backend (which forwards to Inventra server-side). Search,
 * active-status and stock-status filters + pagination. No write actions —
 * Inventra stays the source of truth; per-row stock quantity is not part of
 * the product response (stock lives in the Inventory view, next step).
 */
export function ProductsPage() {
  const [data, setData] = useState<ProductListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [isActive, setIsActive] = useState<ActiveFilter>("");
  const [stockStatus, setStockStatus] = useState("");

  const load = useCallback(
    async (
      targetPage: number,
      targetSearch: string,
      targetActive: ActiveFilter,
      targetStock: string,
    ) => {
      setLoading(true);
      setError(null);
      try {
        const result = await fetchAdminProducts({
          page: targetPage,
          search: targetSearch || null,
          is_active: targetActive === "" ? null : targetActive === "true",
          stock_status: targetStock || null,
        });
        setData(result);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load products");
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void load(page, search, isActive, stockStatus);
  }, [load, page, search, isActive, stockStatus]);

  function applySearch(event: React.FormEvent) {
    event.preventDefault();
    setPage(1);
    setSearch(searchInput.trim());
  }

  const pages = data?.pages ?? 1;

  return (
    <section className="products-pane">
      <header className="pane-header">
        <h2>Products</h2>
        <span className="count">{data ? `${data.total} total` : ""}</span>
      </header>

      <form className="products-filters" onSubmit={applySearch}>
        <input
          type="search"
          placeholder="Search products…"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          aria-label="Search products"
        />
        <select
          value={isActive}
          onChange={(e) => {
            setIsActive(e.target.value as ActiveFilter);
            setPage(1);
          }}
          aria-label="Filter by status"
        >
          <option value="">All statuses</option>
          <option value="true">Active</option>
          <option value="false">Inactive</option>
        </select>
        <select
          value={stockStatus}
          onChange={(e) => {
            setStockStatus(e.target.value);
            setPage(1);
          }}
          aria-label="Filter by stock level"
        >
          <option value="">All stock levels</option>
          <option value="in_stock">In stock</option>
          <option value="low_stock">Low stock</option>
          <option value="out_of_stock">Out of stock</option>
        </select>
        <button type="submit">Search</button>
      </form>

      {loading && data === null && <p className="state">Loading products…</p>}

      {error && (
        <div className="state error">
          <p role="alert">{error}</p>
          <button onClick={() => void load(page, search, isActive, stockStatus)}>
            Retry
          </button>
        </div>
      )}

      {!loading && !error && data?.products.length === 0 && (
        <p className="state">No products found.</p>
      )}

      {data && data.products.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Product</th>
                <th>SKU</th>
                <th>Category</th>
                <th className="num">Price</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.products.map((product) => (
                <tr key={product.id}>
                  <td>
                    <strong>{product.name}</strong>
                    {product.description && (
                      <span className="table-sub">{product.description}</span>
                    )}
                  </td>
                  <td className="mono">{product.sku}</td>
                  <td>{product.category_name ?? "—"}</td>
                  <td className="num">{formatPrice(product.price)}</td>
                  <td>
                    <span className={`badge ${product.is_active ? "active" : "inactive"}`}>
                      {product.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data && pages > 1 && (
        <footer className="pager">
          <button
            disabled={loading || page <= 1}
            onClick={() => setPage(page - 1)}
          >
            ← Prev
          </button>
          <span>
            {page} / {pages}
          </span>
          <button
            disabled={loading || page >= pages}
            onClick={() => setPage(page + 1)}
          >
            Next →
          </button>
        </footer>
      )}
    </section>
  );
}

export default ProductsPage;
