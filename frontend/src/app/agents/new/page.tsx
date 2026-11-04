"use client";

// Create-agent wizard: name + description, paste a document, pick tools.
// On submit we POST to the gateway (which starts the provisioning saga) and
// redirect to the detail page to watch it provision.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/store";

const AVAILABLE_TOOLS = ["calculator", "word_count"];

export default function NewAgentPage() {
  const router = useRouter();
  const token = useAuth((s) => s.token);
  const hasHydrated = useAuth((s) => s.hasHydrated);
  const qc = useQueryClient();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [docName, setDocName] = useState("knowledge.txt");
  const [docContent, setDocContent] = useState("");
  const [tools, setTools] = useState<string[]>([]);

  useEffect(() => {
    if (hasHydrated && !token) router.replace("/");
  }, [hasHydrated, token, router]);

  const mutation = useMutation({
    mutationFn: () =>
      api.createAgent(token!, {
        name,
        description,
        documents: docContent.trim()
          ? [{ filename: docName, content: docContent }]
          : [],
        tool_names: tools,
      }),
    onSuccess: (agent) => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      router.push(`/agents/${agent.id}`);
    },
  });

  if (!hasHydrated || !token) return null;

  return (
    <main className="container" style={{ maxWidth: 640 }}>
      <Link href="/agents" className="muted" style={{ display: "inline-block", marginBottom: 14 }}>
        ← Back to agents
      </Link>
      <h1 className="title-lg" style={{ marginBottom: 4 }}>New Agent</h1>
      <p className="muted" style={{ marginBottom: 18 }}>
        Give it a name, optionally attach a knowledge document and tools, then
        provision.
      </p>

      <div className="card">
        <label className="label">Name</label>
        <input
          placeholder="e.g. Support Assistant"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />

        <label className="label">Description</label>
        <input
          placeholder="What does this agent do?"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />

        <label className="label">Knowledge document (optional)</label>
        <input
          placeholder="filename.txt"
          value={docName}
          onChange={(e) => setDocName(e.target.value)}
        />
        <textarea
          rows={6}
          style={{ marginTop: 8 }}
          placeholder="Paste internal docs here. They'll be chunked, embedded, and retrieved to ground answers."
          value={docContent}
          onChange={(e) => setDocContent(e.target.value)}
        />

        <label className="label">Tools</label>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 4 }}>
          {AVAILABLE_TOOLS.map((t) => {
            const selected = tools.includes(t);
            return (
              <label key={t} className={`chip ${selected ? "selected" : ""}`}>
                <input
                  type="checkbox"
                  checked={selected}
                  onChange={(e) =>
                    setTools(
                      e.target.checked
                        ? [...tools, t]
                        : tools.filter((x) => x !== t)
                    )
                  }
                />
                {t}
              </label>
            );
          })}
        </div>

        {mutation.isError && (
          <p className="error" style={{ marginTop: 14 }}>
            Failed to create agent. Try again.
          </p>
        )}

        <div className="row" style={{ marginTop: 22, justifyContent: "flex-end" }}>
          <button
            className="btn ghost"
            onClick={() => router.push("/agents")}
          >
            Cancel
          </button>
          <button
            className="btn"
            disabled={!name || mutation.isPending}
            onClick={() => mutation.mutate()}
            style={{ justifyContent: "center" }}
          >
            {mutation.isPending && <span className="spinner" />}
            {mutation.isPending ? "Provisioning…" : "Create & provision"}
          </button>
        </div>
      </div>
    </main>
  );
}
