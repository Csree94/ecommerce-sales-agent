import { useCallback, useEffect, useState } from "react";
import { ApiError, fetchConversations } from "./api";
import { displayName, formatTime } from "./display";
import type { ConversationSummary } from "./types";

/** LEFT pane: paginated conversation list ordered by most recent activity. */
export function ConversationList({
  selectedId,
  onSelect,
}: {
  selectedId: string | null;
  onSelect: (conversation: ConversationSummary) => void;
}) {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (targetPage: number) => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchConversations(targetPage);
      setConversations(data.items);
      setTotal(data.total);
      setPage(data.page);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load conversations");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(1);
  }, [load]);

  const totalPages = Math.max(1, Math.ceil(total / 25));

  return (
    <section className="list-pane">
      <header className="pane-header">
        <h2>Conversations</h2>
        <button onClick={() => void load(page)} disabled={loading} title="Refresh">
          ↻
        </button>
      </header>

      {loading && conversations.length === 0 && <p className="state">Loading…</p>}

      {!loading && error && (
        <div className="state error">
          <p role="alert">{error}</p>
          <button onClick={() => void load(1)}>Retry</button>
        </div>
      )}

      {!loading && !error && conversations.length === 0 && (
        <p className="state">No conversations yet.</p>
      )}

      <ul className="conversation-items">
        {conversations.map((conversation) => (
          <li key={conversation.id}>
            <button
              className={`conversation-row${conversation.id === selectedId ? " selected" : ""}`}
              onClick={() => onSelect(conversation)}
            >
              <span className="row-top">
                <strong>{displayName(conversation.customer)}</strong>
                <time>{formatTime(conversation.last_message_at)}</time>
              </span>
              <span className="row-preview">
                {conversation.last_message_preview ?? "No messages yet"}
              </span>
              <span className={`badge ${conversation.status}`}>{conversation.status}</span>
            </button>
          </li>
        ))}
      </ul>

      {totalPages > 1 && (
        <footer className="pager">
          <button disabled={page <= 1} onClick={() => void load(page - 1)}>
            ← Prev
          </button>
          <span>
            {page} / {totalPages}
          </span>
          <button disabled={page >= totalPages} onClick={() => void load(page + 1)}>
            Next →
          </button>
        </footer>
      )}
    </section>
  );
}

export default ConversationList;
