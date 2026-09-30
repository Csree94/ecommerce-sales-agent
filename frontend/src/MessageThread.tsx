import { useEffect, useRef, useState } from "react";
import { fetchMessages } from "./api";
import { displayName, formatClock } from "./display";
import type { ConversationSummary, MessagesPage } from "./types";

/** RIGHT pane: WhatsApp-style chronological thread for the selected conversation. */
export function MessageThread({ conversation }: { conversation: ConversationSummary | null }) {
  const [data, setData] = useState<MessagesPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!conversation) {
      setData(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchMessages(conversation.id)
      .then(setData)
      .catch((err: unknown) => {
        if ((err as { name?: string }).name === "AbortError") {
          return;
        }
        setError(err instanceof Error ? err.message : "Failed to load messages");
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation?.id]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [data]);

  if (!conversation) {
    return (
      <section className="thread-pane">
        <div className="state">Select a conversation to view the chat.</div>
      </section>
    );
  }

  return (
    <section className="thread-pane">
      <header className="pane-header">
        <h2>{displayName(conversation.customer)}</h2>
        <span className={`badge ${conversation.status}`}>{conversation.status}</span>
      </header>

      {loading && <p className="state">Loading messages…</p>}
      {error && (
        <p className="state error" role="alert">
          {error}
        </p>
      )}
      {data && data.messages.length === 0 && (
        <p className="state">No messages in this conversation yet.</p>
      )}

      <div className="messages">
        {data?.messages.map((message) => (
          <div key={message.id} className={`bubble-row ${message.role}`}>
            <div className={`bubble ${message.role}`}>
              <p>{message.content}</p>
              <time>{formatClock(message.created_at)}</time>
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </section>
  );
}

export default MessageThread;
