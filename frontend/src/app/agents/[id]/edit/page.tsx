"use client";

// Edit an existing agent.
//
// Two kinds of change, handled differently on purpose:
//   * name, description, tools, removing a document -> applied instantly.
//   * adding a document -> needs embeddings, so the gateway re-runs the
//     provisioning saga. The agent shows PROVISIONING for a few seconds, then
//     ACTIVE again. If indexing fails, only the new document is rolled back.
//
// The form is compared with the agent as it was loaded (see lib/agentDiff.ts)
// and only the differences are sent.

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  buildUpdate,
  formFromAgent,
  needsReindex,
  type EditForm,
} from "@/lib/agentDiff";
import { useAuth } from "@/lib/store";
import { useToast } from "@/components/Toast";

const AVAILABLE_TOOLS = ["calculator", "word_count"];

export default function EditAgentPage() {
  const router = useRouter();
  const { id } = useParams<{ id: string }>();
  const token = useAuth((s) => s.token);
  const hasHydrated = useAuth((s) => s.hasHydrated);
  const qc = useQueryClient();
  const toast = useToast();

  const [form, setForm] = useState<EditForm | null>(null);

  useEffect(() => {
    if (hasHydrated && !token) router.replace("/");
  }, [hasHydrated, token, router]);

  // Always load fresh data for this page: the form is built from it, and a stale
  // document list would let the user "remove" a document that no longer exists.
  const { data: agent, isFetchedAfterMount } = useQuery({
    queryKey: ["agent", id],
    queryFn: () => api.getAgent(token!, id),
    enabled: !!token,
    refetchOnMount: "always",
  });

  // Only an ACTIVE agent can be edited; otherwise go back to its page.
  useEffect(() => {
    if (agent && isFetchedAfterMount && agent.status !== "ACTIVE") {
      router.replace(`/agents/${id}`);
    }
  }, [agent, isFetchedAfterMount, id, router]);

  // Fill the form once, from the fresh data.
  useEffect(() => {
    if (agent && isFetchedAfterMount && agent.status === "ACTIVE" && form === null) {
      setForm(formFromAgent(agent));
    }
  }, [agent, isFetchedAfterMount, form]);

  const update = agent && form ? buildUpdate(agent, form) : null;

  const save = useMutation({
    mutationFn: () => api.updateAgent(token!, id, update!),
    onSuccess: (saved) => {
      qc.setQueryData(["agent", id], saved);
      qc.invalidateQueries({ queryKey: ["agents"] });
      toast.show(
        saved.status === "PROVISIONING"
          ? "Saved. Indexing the new document…"
          : "Changes saved",
        "success"
      );
      router.push(`/agents/${id}`);
    },
  });

  if (!hasHydrated || !token || !agent || !form) return null;

  const patch = (changes: Partial<EditForm>) =>
    setForm((f) => (f ? { ...f, ...changes } : f));

  const toggleRemoved = (docId: string) =>
    patch({
      removedDocIds: form.removedDocIds.includes(docId)
        ? form.removedDocIds.filter((x) => x !== docId)
        : [...form.removedDocIds, docId],
    });

  const reindex = needsReindex(update);

  return (
    <main className="container" style={{ maxWidth: 640 }}>
      <Link
        href={`/agents/${id}`}
        className="muted"
        style={{ display: "inline-block", marginBottom: 14 }}
      >
        ← Back to {agent.name}
      </Link>
      <h1 className="title-lg" style={{ marginBottom: 4 }}>Edit agent</h1>
      <p className="muted" style={{ marginBottom: 18 }}>
        Change its name, description, tools or knowledge. Only what you change
        is saved.
      </p>

      <div className="card">
        <label className="label">Name</label>
        <input value={form.name} onChange={(e) => patch({ name: e.target.value })} />

        <label className="label">Description</label>
        <input
          placeholder="What does this agent do?"
          value={form.description}
          onChange={(e) => patch({ description: e.target.value })}
        />

        <label className="label">Knowledge documents</label>
        {agent.documents.length === 0 ? (
          <p className="muted">No documents yet.</p>
        ) : (
          <ul className="doc-list">
            {agent.documents.map((d) => {
              const removed = form.removedDocIds.includes(d.id);
              return (
                <li key={d.id} className={`doc-row ${removed ? "removed" : ""}`}>
                  <div>
                    <strong>{d.filename}</strong>
                    <span className="faint">
                      {" "}· {d.characters.toLocaleString()} characters
                    </span>
                    {removed && <span className="faint"> · will be removed</span>}
                  </div>
                  <button
                    type="button"
                    className="btn ghost btn-sm"
                    onClick={() => toggleRemoved(d.id)}
                  >
                    {removed ? "Undo" : "Remove"}
                  </button>
                </li>
              );
            })}
          </ul>
        )}

        <label className="label">Add a document (optional)</label>
        <input
          placeholder="filename.txt"
          value={form.newDocName}
          onChange={(e) => patch({ newDocName: e.target.value })}
        />
        <textarea
          rows={5}
          style={{ marginTop: 8 }}
          placeholder="Paste the new document here. It will be chunked, embedded and added to this agent's knowledge."
          value={form.newDocContent}
          onChange={(e) => patch({ newDocContent: e.target.value })}
        />

        <label className="label">Tools</label>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 4 }}>
          {AVAILABLE_TOOLS.map((t) => {
            const selected = form.tools.includes(t);
            return (
              <label key={t} className={`chip ${selected ? "selected" : ""}`}>
                <input
                  type="checkbox"
                  checked={selected}
                  onChange={(e) =>
                    patch({
                      tools: e.target.checked
                        ? [...form.tools, t]
                        : form.tools.filter((x) => x !== t),
                    })
                  }
                />
                {t}
              </label>
            );
          })}
        </div>

        {reindex && (
          <p className="note" role="note">
            Adding a document re-indexes the agent. It will be unavailable for a
            few seconds while the document is embedded.
          </p>
        )}

        {save.isError && (
          <p className="error" style={{ marginTop: 14 }} role="alert">
            {save.error instanceof Error ? save.error.message : "Could not save."}
          </p>
        )}

        <div className="row" style={{ marginTop: 22, justifyContent: "flex-end" }}>
          <button className="btn ghost" onClick={() => router.push(`/agents/${id}`)}>
            Cancel
          </button>
          <button
            className="btn"
            disabled={!update || !form.name.trim() || save.isPending}
            onClick={() => save.mutate()}
            style={{ justifyContent: "center" }}
          >
            {save.isPending && <span className="spinner" />}
            {save.isPending ? "Saving…" : reindex ? "Save & re-index" : "Save changes"}
          </button>
        </div>
      </div>
    </main>
  );
}
