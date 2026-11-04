"use client";

// Agent detail + chat. Polls status until ACTIVE, then enables chat with
// modern bubbles, a typing indicator, and auto-scroll. The conversation lives in
// the database (GET /messages), so it survives a refresh and a new login; the
// screen just shows what the server has.

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { api, type Source } from "@/lib/api";
import { useAuth } from "@/lib/store";
import { Modal } from "@/components/Modal";
import { useToast } from "@/components/Toast";

function SourceList({ sources }: { sources: Source[] }) {
  return (
    <details className="chat-sources">
      {/* These are the passages retrieved for the question, not a guarantee
          that the model used each one, so we do not call them "sources". */}
      <summary>
        📎 {sources.length} retrieved passage{sources.length > 1 ? "s" : ""}
      </summary>
      <ul>
        {sources.map((s, i) => (
          <li key={i}>
            <div className="src-head">
              <strong>{s.filename || "document"}</strong>
              <span> · part {s.chunk_index + 1}</span>
              {s.score !== null && (
                <span> · {(s.score * 100).toFixed(0)}% match</span>
              )}
            </div>
            <div className="src-snippet">{s.snippet}</div>
          </li>
        ))}
      </ul>
    </details>
  );
}

export default function AgentDetailPage() {
  const router = useRouter();
  const { id } = useParams<{ id: string }>();
  const token = useAuth((s) => s.token);
  const hasHydrated = useAuth((s) => s.hasHydrated);
  const toast = useToast();
  const queryClient = useQueryClient();
  const [question, setQuestion] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (hasHydrated && !token) router.replace("/");
  }, [hasHydrated, token, router]);

  const { data: agent } = useQuery({
    queryKey: ["agent", id],
    queryFn: () => api.getAgent(token!, id),
    enabled: !!token,
    refetchInterval: (q) =>
      q.state.data?.status === "PROVISIONING" ? 2000 : false,
  });

  // The saved conversation. The server is the source of truth.
  const messagesKey = ["messages", id];
  const { data: messages = [] } = useQuery({
    queryKey: messagesKey,
    queryFn: () => api.getMessages(token!, id),
    enabled: !!token,
  });

  const ask = useMutation({
    mutationFn: (q: string) => api.queryAgent(token!, id, q),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: messagesKey }),
    onError: (_error, q) => {
      setQuestion((current) => current || q);
      toast.show("The agent couldn’t answer. Try again.", "error");
    },
  });

  const clear = useMutation({
    mutationFn: () => api.clearMessages(token!, id),
    onSuccess: () => {
      queryClient.setQueryData(messagesKey, []);
      setConfirmClear(false);
    },
    onError: () => toast.show("Couldn’t clear the chat. Try again.", "error"),
  });

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length, ask.isPending]);

  if (!hasHydrated || !token || !agent) return null;

  return (
    <main className="container" style={{ maxWidth: 760 }}>
      <Link href="/agents" className="muted" style={{ display: "inline-block", marginBottom: 14 }}>
        ← Back to agents
      </Link>

      <div className="row" style={{ marginBottom: 6 }}>
        <h1 className="title-lg">{agent.name}</h1>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          {agent.status === "ACTIVE" && (
            <Link className="btn secondary btn-sm" href={`/agents/${id}/edit`}>
              Edit
            </Link>
          )}
          <span className={`badge ${agent.status}`}>{agent.status}</span>
        </div>
      </div>
      {agent.description && <p className="muted">{agent.description}</p>}

      {(agent.documents.length > 0 || agent.tool_names.length > 0) && (
        <div className="meta-list" aria-label="Knowledge and tools">
          {agent.documents.map((d) => (
            <span key={d.id} className="tag" title={`${d.characters.toLocaleString()} characters`}>
              📄 {d.filename}
            </span>
          ))}
          {agent.tool_names.map((t) => (
            <span key={t} className="tag">🛠 {t}</span>
          ))}
        </div>
      )}

      {agent.status === "PROVISIONING" && (
        <div className="card" style={{ marginTop: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span className="spinner" style={{ borderTopColor: "var(--warn)", borderColor: "rgba(241,180,76,.3)" }} />
            <div>
              <strong>Provisioning…</strong>
              <div className="muted">
                Documents are being ingested and the agent activated. This
                updates automatically.
              </div>
            </div>
          </div>
        </div>
      )}

      {agent.status === "FAILED" && (
        <div className="card" style={{ marginTop: 16, borderColor: "rgba(255,107,107,.4)" }}>
          <strong style={{ color: "var(--err)" }}>Provisioning failed</strong>
          <p className="muted">
            {agent.last_saga_error
              ? `Reason: ${agent.last_saga_error}. `
              : ""}
            The saga rolled back all partial work.
          </p>
        </div>
      )}

      {/* An ACTIVE agent whose latest saga FAILED means an edit was rolled back:
          the agent kept working with what it had before. Tell the user. */}
      {agent.status === "ACTIVE" && agent.last_saga_status === "FAILED" && (
        <div className="card" style={{ marginTop: 16, borderColor: "rgba(241,180,76,.45)" }}>
          <strong style={{ color: "var(--warn)" }}>Your last change was not applied</strong>
          <p className="muted">
            {agent.last_saga_error ? `Reason: ${agent.last_saga_error}. ` : ""}
            The new document was removed and the agent kept working with its
            previous knowledge.
          </p>
        </div>
      )}

      {agent.status === "ACTIVE" && (
        <>
          <div className="card" style={{ marginTop: 16, padding: 12 }}>
            {messages.length > 0 && (
              <div className="row" style={{ marginBottom: 8 }}>
                <span className="faint">
                  Conversation saved · the agent remembers the last few messages
                </span>
                <button
                  type="button"
                  className="btn ghost btn-sm"
                  onClick={() => setConfirmClear(true)}
                >
                  Clear chat
                </button>
              </div>
            )}
            <div className="chat-window" ref={scrollRef}>
              {messages.length === 0 && !ask.isPending && (
                <div className="empty" style={{ border: "none", padding: "32px 16px" }}>
                  <span className="emoji">💬</span>
                  Ask a question. Try arithmetic (e.g. “what is 21 * 2?”) if the
                  calculator tool is attached, or ask about your uploaded docs.
                </div>
              )}
              {messages.map((m) => (
                <div key={m.id} className={`chat-msg ${m.role}`}>
                  {m.role === "agent" ? (
                    // react-markdown does not render raw HTML, so model output
                    // cannot inject markup.
                    <div className="markdown">
                      <ReactMarkdown>{m.content}</ReactMarkdown>
                    </div>
                  ) : (
                    m.content
                  )}
                  {m.sources.length > 0 && <SourceList sources={m.sources} />}
                </div>
              ))}
              {ask.isPending && (
                <>
                  {/* Show the question at once; the server stores it with the answer. */}
                  <div className="chat-msg user">{ask.variables}</div>
                  <div className="chat-msg agent" style={{ padding: 0 }}>
                    <div className="typing">
                      <span /><span /><span />
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>

          <form
            style={{ display: "flex", gap: 10, marginTop: 12 }}
            onSubmit={(e) => {
              e.preventDefault();
              const q = question.trim();
              if (!q || ask.isPending) return;
              setQuestion("");
              ask.mutate(q);
            }}
          >
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="Ask the agent…"
              style={{ margin: 0 }}
            />
            <button
              className="btn"
              disabled={ask.isPending || !question.trim()}
              style={{ justifyContent: "center" }}
            >
              {ask.isPending ? <span className="spinner" /> : "Send"}
            </button>
          </form>
        </>
      )}

      <Modal
        open={confirmClear}
        title="Clear this conversation?"
        confirmLabel="Clear chat"
        destructive
        busy={clear.isPending}
        onCancel={() => setConfirmClear(false)}
        onConfirm={() => clear.mutate()}
      >
        The messages are deleted and the agent forgets them. The agent&apos;s
        documents and settings are not affected.
      </Modal>
    </main>
  );
}
