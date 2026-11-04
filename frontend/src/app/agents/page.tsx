"use client";

// Agents list: skeletons while loading, rich cards, modal-confirm delete with
// toast feedback, and pagination.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, type Agent } from "@/lib/api";
import { useAuth } from "@/lib/store";
import { Modal } from "@/components/Modal";
import { useToast } from "@/components/Toast";

const PAGE_SIZE = 10;

export default function AgentsPage() {
  const router = useRouter();
  const token = useAuth((s) => s.token);
  const hasHydrated = useAuth((s) => s.hasHydrated);
  const queryClient = useQueryClient();
  const toast = useToast();
  const [offset, setOffset] = useState(0);
  const [pendingDelete, setPendingDelete] = useState<Agent | null>(null);

  useEffect(() => {
    if (hasHydrated && !token) router.replace("/");
  }, [hasHydrated, token, router]);

  const { data, isLoading, error } = useQuery({
    queryKey: ["agents", offset],
    queryFn: () => api.listAgents(token!, PAGE_SIZE, offset),
    enabled: !!token,
    refetchInterval: 4000,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteAgent(token!, id),
    onSuccess: (_data, _id) => {
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      toast.show(`Deleted "${pendingDelete?.name}"`, "success");
      setPendingDelete(null);
    },
    onError: () => {
      toast.show("Could not delete the agent", "error");
      setPendingDelete(null);
    },
  });

  if (!hasHydrated || !token) return null;

  return (
    <main className="container">
      <div className="row" style={{ marginBottom: 20 }}>
        <div>
          <h1 className="title-lg">My Agents</h1>
          <p className="muted">Create, provision and chat with your AI agents.</p>
        </div>
        <Link className="btn" href="/agents/new">
          + New Agent
        </Link>
      </div>

      {isLoading && (
        <div>
          <div className="skeleton" />
          <div className="skeleton" />
          <div className="skeleton" />
        </div>
      )}

      {error && !(error instanceof ApiError && error.status === 401) && (
        <div className="card">
          <p className="error">Couldn’t load your agents.</p>
          <button
            className="btn secondary btn-sm"
            style={{ marginTop: 10 }}
            onClick={() =>
              queryClient.invalidateQueries({ queryKey: ["agents"] })
            }
          >
            Retry
          </button>
        </div>
      )}

      {!isLoading && data?.items.length === 0 && (
        <div className="empty">
          <span className="emoji">🤖</span>
          No agents yet.
          <div style={{ marginTop: 14 }}>
            <Link className="btn" href="/agents/new">
              Create your first agent
            </Link>
          </div>
        </div>
      )}

      {data?.items.map((agent) => (
        <div key={agent.id} className="card card-interactive">
          <div className="row">
            <Link
              href={`/agents/${agent.id}`}
              style={{ flex: 1, color: "inherit" }}
            >
              <strong style={{ fontSize: 15.5 }}>{agent.name}</strong>
              <div className="muted">
                {agent.description || "No description"}
              </div>
            </Link>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <span className={`badge ${agent.status}`}>{agent.status}</span>
              <button
                className="btn danger btn-sm"
                title="Delete agent"
                onClick={() => setPendingDelete(agent)}
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      ))}

      {data && data.total > PAGE_SIZE && (
        <div className="row" style={{ marginTop: 8 }}>
          <button
            className="btn secondary btn-sm"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            ← Previous
          </button>
          <span className="faint">
            {offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} of {data.total}
          </span>
          <button
            className="btn secondary btn-sm"
            disabled={offset + PAGE_SIZE >= data.total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next →
          </button>
        </div>
      )}

      <Modal
        open={!!pendingDelete}
        title={`Delete "${pendingDelete?.name}"?`}
        confirmLabel="Delete"
        destructive
        busy={deleteMutation.isPending}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() =>
          pendingDelete && deleteMutation.mutate(pendingDelete.id)
        }
      >
        This permanently removes the agent, its documents and vectors. This
        action cannot be undone.
      </Modal>
    </main>
  );
}
