/** Small display helpers shared by the dashboard components. */

import type { CustomerInfo } from "./types";

export function displayName(customer: CustomerInfo): string {
  const name = [customer.first_name, customer.last_name]
    .filter((part) => part && part.trim())
    .join(" ")
    .trim();
  if (name) {
    return name;
  }
  if (customer.username) {
    return `@${customer.username}`;
  }
  return `Customer ${customer.id.slice(0, 8)}`;
}

export function formatTime(iso: string | null): string {
  if (!iso) {
    return "";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
