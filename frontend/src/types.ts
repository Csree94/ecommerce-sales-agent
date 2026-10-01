/** API payload types (mirrors backend/app/api/routes/admin.py response models). */

export interface CustomerInfo {
  id: string;
  username: string | null;
  first_name: string | null;
  last_name: string | null;
  locale: string | null;
}

export interface ConversationSummary {
  id: string;
  status: string;
  channel: string;
  last_message_at: string | null;
  created_at: string;
  last_message_preview: string | null;
  customer: CustomerInfo;
}

export interface ConversationPage {
  items: ConversationSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface MessageOut {
  id: string;
  role: "customer" | "agent" | "system" | "admin";
  content: string;
  created_at: string;
  external_created_at: string | null;
  correlation_id: string | null;
}

export interface MessagesPage {
  conversation: ConversationSummary;
  messages: MessageOut[];
  total: number;
  limit: number;
  offset: number;
}

/** Inventra product contracts (mirrors backend/app/integrations/inventra/schemas.py). */
export interface Product {
  id: number;
  name: string;
  sku: string;
  description: string | null;
  category_id: number | null;
  category_name: string | null;
  price: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ProductListResponse {
  products: Product[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
}

/** Inventra inventory/audit contracts (mirrors the Inventra response shapes). */
export interface InventoryItem {
  id: number;
  product_id: number;
  product_name: string | null;
  product_sku: string | null;
  quantity: number;
  low_stock_threshold: number;
  is_low_stock: boolean;
  updated_at: string;
}

export type MovementType = "STOCK_IN" | "STOCK_OUT" | "STOCK_ADJUSTMENT";

export interface StockMovement {
  id: number;
  product_id: number;
  product_name: string | null;
  user_id: number;
  username: string | null;
  movement_type: MovementType;
  quantity: number;
  notes: string | null;
  created_at: string;
}

export interface StockMovementListResponse {
  movements: StockMovement[];
  total: number;
  page: number;
  per_page: number;
}

export interface Category {
  id: number;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface DashboardMovement {
  id: number;
  product_name: string;
  movement_type: MovementType;
  quantity: number;
  username: string | null;
  notes: string | null;
  created_at: string | null;
}

export interface LowStockProduct {
  product_id: number;
  product_name: string;
  product_sku: string;
  quantity: number;
  threshold: number;
}

export interface InventraDashboardStats {
  total_products: number;
  active_products: number;
  total_stock: number;
  low_stock_count: number;
  out_of_stock_count: number;
  total_stock_in: number;
  total_stock_out: number;
  recent_movements: DashboardMovement[];
  low_stock_products: LowStockProduct[];
}
