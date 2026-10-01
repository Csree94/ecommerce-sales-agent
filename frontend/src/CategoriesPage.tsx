import { useCallback, useEffect, useState } from "react";
import { fetchAdminCategories } from "./api";
import type { Category } from "./types";

/** Categories view (read-only): Inventra's catalog categories. */
export function CategoriesPage() {
  const [categories, setCategories] = useState<Category[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setCategories(await fetchAdminCategories());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load categories");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="products-pane">
      <header className="pane-header">
        <h2>Categories</h2>
        <span className="count">{categories ? `${categories.length} total` : ""}</span>
      </header>

      {loading && categories === null && <p className="state">Loading categories…</p>}

      {error && (
        <div className="state error">
          <p role="alert">{error}</p>
          <button onClick={() => void load()}>Retry</button>
        </div>
      )}

      {!loading && !error && categories?.length === 0 && (
        <p className="state">No categories found.</p>
      )}

      {categories && categories.length > 0 && (
        <div className="category-grid">
          {categories.map((category) => (
            <div key={category.id} className="category-card">
              <h3>{category.name}</h3>
              {category.description && <p>{category.description}</p>}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export default CategoriesPage;
