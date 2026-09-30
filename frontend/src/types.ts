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
