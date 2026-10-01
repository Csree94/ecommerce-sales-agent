/**
 * Admin API client: JWT in sessionStorage + thin fetch helpers.
 *
 * sessionStorage keeps the token out of persistent storage for this project
 * scope; every authenticated request attaches Authorization: Bearer <JWT>.
 * All backend calls go through the Vite dev proxy (/api → http://127.0.0.1:8000).
 */

import type {
  Category,
  ConversationPage,
  InventraDashboardStats,
  InventoryItem,
  MessagesPage,
  ProductListResponse,
  StockMovementListResponse,
} from "./types";

const API_BASE = "/api/v1";
const TOKEN_KEY = "admin_access_token";
const USERNAME_KEY = "admin_username";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function getStoredToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function getStoredUsername(): string | null {
  return sessionStorage.getItem(USERNAME_KEY);
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getStoredToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // Non-JSON error body — keep the generic detail.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export async function login(username: string, password: string): Promise<void> {
  const data = await request<{ access_token: string; token_type: string }>(
    "/admin/login",
    { method: "POST", body: JSON.stringify({ username, password }) },
  );
  sessionStorage.setItem(TOKEN_KEY, data.access_token);
  sessionStorage.setItem(USERNAME_KEY, username);
}

export function logout(): void {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USERNAME_KEY);
}

export function fetchConversations(page = 1): Promise<ConversationPage> {
  return request<ConversationPage>(`/admin/conversations?page=${page}&page_size=25`);
}

export function fetchMessages(conversationId: string): Promise<MessagesPage> {
  return request<MessagesPage>(
    `/admin/conversations/${conversationId}/messages?limit=100&offset=0`,
  );
}

/** Query for the read-only Inventra product proxy (empty/null values are omitted). */
export interface AdminProductQuery {
  page: number;
  search: string | null;
  is_active: boolean | null;
  stock_status: string | null;
}

function buildQueryString(
  entries: Array<[string, string | number | boolean | null]>,
): string {
  const params = new URLSearchParams();
  for (const [key, value] of entries) {
    if (value !== null && value !== "") {
      params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

/** Products via the Project 1 backend proxy (Inventra token stays server-side). */
export function fetchAdminProducts(
  query: AdminProductQuery,
): Promise<ProductListResponse> {
  return request<ProductListResponse>(
    `/admin/products${buildQueryString([
      ["page", query.page],
      ["search", query.search],
      ["is_active", query.is_active],
      ["stock_status", query.stock_status],
    ])}`,
  );
}

/** Inventory levels via the Project 1 backend proxy (read-only). */
export function fetchAdminInventory(query: {
  search: string | null;
  stock_status: string | null;
  low_stock_only: boolean;
}): Promise<InventoryItem[]> {
  return request<InventoryItem[]>(
    `/admin/inventory${buildQueryString([
      ["search", query.search],
      ["stock_status", query.stock_status],
      ["low_stock_only", query.low_stock_only ? "true" : null],
    ])}`,
  );
}

/** Stock-movement history via the Project 1 backend proxy (read-only). */
export function fetchAdminMovements(query: {
  page: number;
  movement_type: string | null;
}): Promise<StockMovementListResponse> {
  return request<StockMovementListResponse>(
    `/admin/inventory/movements${buildQueryString([
      ["page", query.page],
      ["movement_type", query.movement_type],
    ])}`,
  );
}

/** Categories via the Project 1 backend proxy (read-only). */
export function fetchAdminCategories(): Promise<Category[]> {
  return request<Category[]>("/admin/categories");
}

/** Inventra aggregate stats via the Project 1 backend proxy (read-only). */
export function fetchAdminDashboardStats(): Promise<InventraDashboardStats> {
  return request<InventraDashboardStats>("/admin/dashboard/stats");
}
