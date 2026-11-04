"use client";

// Login / register page.

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/store";

export default function LoginPage() {
  const router = useRouter();
  const setAuth = useAuth((s) => s.setAuth);
  const sessionExpired = useAuth((s) => s.sessionExpired);
  const clearExpired = useAuth((s) => s.clearExpired);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    clearExpired();
    setBusy(true);
    try {
      if (mode === "register") {
        await api.register(email, password);
      }
      const token = await api.login(email, password);
      setAuth(token, email);
      router.push("/agents");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main
      className="container"
      style={{
        maxWidth: 420,
        minHeight: "calc(100vh - 120px)",
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
      }}
    >
      <div style={{ textAlign: "center", marginBottom: 20 }}>
        <div
          className="logo"
          style={{ width: 48, height: 48, margin: "0 auto 14px", fontSize: 22 }}
        >
          ◆
        </div>
        <h1 className="title-lg">
          Welcome to <span className="gradient-text">Agent Studio</span>
        </h1>
        <p className="muted">Build and operate your own AI agents.</p>
      </div>

      <div className="card">
        <h2>{mode === "login" ? "Sign in" : "Create account"}</h2>
        {sessionExpired && (
          <p className="muted" style={{ color: "var(--warn)", marginTop: 6 }}>
            Your session expired. Please sign in again.
          </p>
        )}
        <form onSubmit={submit} style={{ marginTop: 10 }}>
          <label className="label">Email</label>
          <input
            type="email"
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
          <label className="label">Password</label>
          <input
            type="password"
            placeholder="At least 8 characters"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={8}
            required
          />
          {error && <p className="error" style={{ marginTop: 10 }}>{error}</p>}
          <button
            className="btn"
            disabled={busy}
            style={{ width: "100%", marginTop: 16, justifyContent: "center" }}
          >
            {busy && <span className="spinner" />}
            {mode === "login" ? "Sign in" : "Create account"}
          </button>
        </form>
        <p className="muted" style={{ marginTop: 16, textAlign: "center" }}>
          {mode === "login" ? "No account yet?" : "Already have an account?"}{" "}
          <a
            href="#"
            onClick={(e) => {
              e.preventDefault();
              setError(null);
              setMode(mode === "login" ? "register" : "login");
            }}
          >
            {mode === "login" ? "Create one" : "Sign in"}
          </a>
        </p>
      </div>
    </main>
  );
}
